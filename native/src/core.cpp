#include "kairix_native.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <stdexcept>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace kairix {
namespace {
#pragma pack(push, 1)
struct Header {
  char magic[8];
  std::uint32_t version, header_bytes, slot_bytes, slots;
  std::uint64_t producer_id, active_generation, created_ns;
};
struct Slot {
  std::uint64_t commit_begin, sequence, producer_id, acquisition_ns, publication_ns;
  std::uint64_t pico_sequence, pico_timestamp_us, pico_receive_ns;
  double x, y, z, pan, tilt, roll, fov;
  std::uint32_t flags;
  std::uint64_t model_generation, reference_generation;
  std::uint32_t checksum;
  std::uint64_t commit_end;
};
#pragma pack(pop)
static_assert(sizeof(Header) == 48, "pose header layout changed");
static_assert(sizeof(Slot) == 152, "pose slot layout changed");
constexpr std::size_t file_size = header_size + slot_count * sizeof(Slot);

std::uint32_t crc32(const std::uint8_t* data, std::size_t length) {
  std::uint32_t crc = 0xffffffffu;
  for (std::size_t i = 0; i < length; ++i) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; ++bit)
      crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
  }
  return ~crc;
}
std::uint64_t monotonic_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
}
double radians(double value) { return value * 3.14159265358979323846 / 180.0; }
double lerp(double a, double b, double t) { return a + (b - a) * t; }
}  // namespace

std::optional<Point2> project_point(const Point3& point, const Pose& camera,
                                    double width, double height) {
  const double pan = radians(camera.pan), tilt = -radians(camera.tilt), roll = -radians(camera.roll);
  double x = point.x - camera.x, y = point.y - camera.y, z = point.z - camera.z;
  const double x1 = x * std::cos(pan) - y * std::sin(pan);
  const double y1 = x * std::sin(pan) + y * std::cos(pan);
  x = x1; y = y1;
  const double y2 = y * std::cos(tilt) - z * std::sin(tilt);
  const double z2 = y * std::sin(tilt) + z * std::cos(tilt);
  y = y2; z = z2;
  const double x3 = x * std::cos(roll) - z * std::sin(roll);
  const double z3 = x * std::sin(roll) + z * std::cos(roll);
  x = x3; z = z3;
  if (y <= 0.05) return std::nullopt;
  const double fov = radians(std::clamp(camera.fov, 5.0, 160.0));
  const double focal = width / (2.0 * std::tan(fov / 2.0));
  return Point2{width / 2.0 + x / y * focal, height / 2.0 - z / y * focal, y, focal / y};
}

double diagnostic_motion(const std::string& mode, double elapsed, double extent) {
  if (mode == "stationary") return 0.0;
  if (mode == "horizontal" || mode == "vertical")
    return std::fmod(elapsed * 160.0, extent * 2.0) - extent;
  if (mode == "sine") return std::sin(elapsed * 2.0 * 3.14159265358979323846 / 4.0) * extent;
  if (mode == "reversal") {
    const double phase = std::fmod(elapsed, 4.0) / 4.0;
    return (phase < 0.5 ? -1.0 + phase * 4.0 : 3.0 - phase * 4.0) * extent;
  }
  return 0.0;
}

Pose interpolate_pose(const Pose& a, const Pose& b, std::uint64_t target) {
  if (b.publication_ns <= a.publication_ns || target <= a.publication_ns) return a;
  if (target >= b.publication_ns) return b;
  const double t = static_cast<double>(target - a.publication_ns) /
                   static_cast<double>(b.publication_ns - a.publication_ns);
  Pose out = b;
  out.x = lerp(a.x, b.x, t); out.y = lerp(a.y, b.y, t); out.z = lerp(a.z, b.z, t);
  out.pan = lerp(a.pan, b.pan, t); out.tilt = lerp(a.tilt, b.tilt, t);
  out.roll = lerp(a.roll, b.roll, t); out.fov = lerp(a.fov, b.fov, t);
  out.publication_ns = target;
  return out;
}

std::optional<DisplayMode> select_exact_1080p50(const std::vector<DisplayMode>& modes) {
  const auto found = std::find_if(modes.begin(), modes.end(), [](const auto& mode) {
    return mode.width == 1920 && mode.height == 1080 && mode.refresh_hz == 50;
  });
  return found == modes.end() ? std::nullopt : std::optional<DisplayMode>(*found);
}

PoseMmapReader::PoseMmapReader(std::string path, double stale_seconds)
    : path_(std::move(path)), stale_ns_(static_cast<std::uint64_t>(std::max(.01, stale_seconds) * 1e9)) {}
PoseMmapReader::~PoseMmapReader() { close(); }
void PoseMmapReader::open() {
  close();
  fd_ = ::open(path_.c_str(), O_RDONLY | O_CLOEXEC);
  if (fd_ < 0) throw std::runtime_error("pose producer unavailable: " + path_);
  struct stat info{};
  if (fstat(fd_, &info) || static_cast<std::size_t>(info.st_size) != file_size) {
    close(); throw std::runtime_error("incompatible pose shared-memory size");
  }
  void* mapped = mmap(nullptr, file_size, PROT_READ, MAP_SHARED, fd_, 0);
  if (mapped == MAP_FAILED) { close(); throw std::runtime_error("cannot map pose shared memory"); }
  memory_ = static_cast<const std::uint8_t*>(mapped);
  const auto* header = reinterpret_cast<const Header*>(memory_);
  if (std::memcmp(header->magic, "KRLPOSE1", 8) || header->version != protocol_version ||
      header->header_bytes != header_size || header->slot_bytes != sizeof(Slot) || header->slots != slot_count) {
    close(); throw std::runtime_error("incompatible pose protocol header");
  }
}
void PoseMmapReader::close() {
  if (memory_) munmap(const_cast<std::uint8_t*>(memory_), file_size);
  memory_ = nullptr;
  if (fd_ >= 0) ::close(fd_);
  fd_ = -1;
}
std::optional<Pose> PoseMmapReader::read_generation(std::uint64_t generation,
                                                    std::uint64_t active, std::string* error) {
  if (!generation || generation > active || active - generation >= slot_count) {
    if (error) *error = "pose generation unavailable"; return std::nullopt;
  }
  Slot slot{};
  std::memcpy(&slot, memory_ + header_size + (generation % slot_count) * sizeof(Slot), sizeof(Slot));
  if (slot.commit_begin != generation || slot.commit_end != generation) {
    if (error) *error = "torn pose snapshot"; return std::nullopt;
  }
  if (crc32(reinterpret_cast<const std::uint8_t*>(&slot), offsetof(Slot, checksum)) != slot.checksum) {
    if (error) *error = "corrupt pose snapshot"; return std::nullopt;
  }
  Pose pose{slot.commit_begin, slot.sequence, slot.producer_id, slot.acquisition_ns,
            slot.publication_ns, slot.pico_sequence, slot.pico_timestamp_us, slot.pico_receive_ns,
            slot.x, slot.y, slot.z, slot.pan, slot.tilt, slot.roll, slot.fov,
            slot.flags, slot.model_generation, slot.reference_generation};
  pose.stale = monotonic_ns() > pose.publication_ns && monotonic_ns() - pose.publication_ns > stale_ns_;
  pose.producer_restart = last_producer_ && last_producer_ != pose.producer_id;
  last_producer_ = pose.producer_id;
  return pose;
}
std::optional<Pose> PoseMmapReader::latest(std::string* error) {
  if (!memory_) { if (error) *error = "pose reader is closed"; return std::nullopt; }
  const auto* header = reinterpret_cast<const Header*>(memory_);
  const std::uint64_t active = header->active_generation;
  std::atomic_thread_fence(std::memory_order_acquire);
  auto pose = read_generation(active, active, error);
  std::atomic_thread_fence(std::memory_order_acquire);
  if (header->active_generation != active) {
    if (error) *error = "pose changed during read"; return std::nullopt;
  }
  return pose;
}
std::vector<Pose> PoseMmapReader::history(std::size_t limit) {
  std::vector<Pose> result;
  if (!memory_) return result;
  const auto active = reinterpret_cast<const Header*>(memory_)->active_generation;
  const auto count = std::min(slot_count, std::max<std::size_t>(1, limit));
  const auto first = active > count ? active - count + 1 : 1;
  for (auto generation = first; generation <= active; ++generation) {
    auto pose = read_generation(generation, active, nullptr);
    if (pose) result.push_back(*pose);
  }
  return result;
}
std::optional<Pose> PoseMmapReader::at_timestamp(std::uint64_t target, std::size_t limit) {
  if (!memory_) return std::nullopt;
  const auto active = reinterpret_cast<const Header*>(memory_)->active_generation;
  const auto count = std::min(slot_count, std::max<std::size_t>(1, limit));
  const auto first = active > count ? active - count + 1 : 1;
  std::optional<Pose> previous;
  for (auto generation = first; generation <= active; ++generation) {
    auto current = read_generation(generation, active, nullptr);
    if (!current) continue;
    if (current->publication_ns >= target)
      return previous ? std::optional<Pose>(interpolate_pose(*previous, *current, target)) : current;
    previous = current;
  }
  return previous;
}
}  // namespace kairix
