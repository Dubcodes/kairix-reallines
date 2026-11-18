#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace kairix {

constexpr std::uint32_t protocol_version = 1;
constexpr std::size_t slot_count = 64;
constexpr std::size_t header_size = 64;

struct Pose {
  std::uint64_t generation{};
  std::uint64_t sequence{};
  std::uint64_t producer_id{};
  std::uint64_t acquisition_ns{};
  std::uint64_t publication_ns{};
  std::uint64_t pico_sequence{};
  std::uint64_t pico_timestamp_us{};
  std::uint64_t pico_receive_ns{};
  double x{}, y{}, z{1.7}, pan{}, tilt{}, roll{}, fov{60.0};
  std::uint32_t flags{};
  std::uint64_t model_generation{};
  std::uint64_t reference_generation{};
  bool stale{};
  bool producer_restart{};
};

struct Point3 { double x{}, y{}, z{}; };
struct Point2 { double x{}, y{}, depth{}, scale{}; };
struct DisplayMode { int width{}, height{}, refresh_hz{}, index{}; };

std::optional<Point2> project_point(const Point3& point, const Pose& camera,
                                    double width, double height);
double diagnostic_motion(const std::string& mode, double elapsed_seconds,
                         double extent_pixels = 600.0);
Pose interpolate_pose(const Pose& before, const Pose& after, std::uint64_t target_ns);
std::optional<DisplayMode> select_exact_1080p50(const std::vector<DisplayMode>& modes);

class PoseMmapReader {
 public:
  explicit PoseMmapReader(std::string path, double stale_seconds = 0.25);
  ~PoseMmapReader();
  PoseMmapReader(const PoseMmapReader&) = delete;
  PoseMmapReader& operator=(const PoseMmapReader&) = delete;
  void open();
  void close();
  std::optional<Pose> latest(std::string* error = nullptr);
  std::vector<Pose> history(std::size_t limit = 16);
  std::optional<Pose> at_timestamp(std::uint64_t target_ns, std::size_t limit = 32);
 private:
  std::optional<Pose> read_generation(std::uint64_t generation, std::uint64_t active,
                                      std::string* error);
  std::string path_;
  std::uint64_t stale_ns_;
  int fd_{-1};
  const std::uint8_t* memory_{nullptr};
  std::uint64_t last_producer_{};
};

}  // namespace kairix
