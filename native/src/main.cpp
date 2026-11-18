#include "kairix_native.hpp"
#include <SDL.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {
using Clock = std::chrono::steady_clock;
std::atomic<bool> running{true}, export_requested{false};
std::uint64_t ns_now() { return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count(); }
double ms(std::uint64_t value) { return static_cast<double>(value) / 1e6; }
void signal_handler(int signal) { if (signal == SIGUSR1) export_requested = true; else running = false; }

struct Options {
  std::string mode{"internal"}, motion{"sine"}, pose_path{"/dev/shm/kairix-reallines-pose-v1"};
  std::string diagnostics{"/run/kairix-native/renderer-diagnostics.json"}, stale_policy{"blank"};
  double interpolation_delay_ms{10.0};
};
Options options(int argc, char** argv) {
  Options value;
  for (int i = 1; i < argc; ++i) {
    const std::string key = argv[i];
    auto next = [&]() { if (++i >= argc) throw std::runtime_error("missing value for " + key); return std::string(argv[i]); };
    if (key == "--mode") value.mode = next(); else if (key == "--motion") value.motion = next();
    else if (key == "--pose-file") value.pose_path = next(); else if (key == "--diagnostics") value.diagnostics = next();
    else if (key == "--stale-policy") value.stale_policy = next();
    else if (key == "--interpolation-delay-ms") value.interpolation_delay_ms = std::stod(next());
    else if (key == "--help") { std::cout << "--mode internal|latest|interpolate --motion stationary|horizontal|vertical|sine|reversal\n"; std::exit(0); }
    else throw std::runtime_error("unknown option: " + key);
  }
  if (value.mode != "internal" && value.mode != "latest" && value.mode != "interpolate") throw std::runtime_error("invalid mode");
  if (value.stale_policy != "blank" && value.stale_policy != "hold") throw std::runtime_error("invalid stale policy");
  if (value.interpolation_delay_ms < 0 || value.interpolation_delay_ms > 100) throw std::runtime_error("interpolation delay must be 0..100 ms");
  return value;
}

struct Ring {
  std::deque<double> values;
  std::size_t capacity{6000};
  void add(double value) { if (values.size() == capacity) values.pop_front(); values.push_back(value); }
  double percentile(double fraction) const { if (values.empty()) return 0; std::vector<double> copy(values.begin(),values.end()); std::sort(copy.begin(),copy.end()); return copy[std::min(copy.size()-1,static_cast<std::size_t>(std::ceil(copy.size()*fraction)-1))]; }
  double mean() const { return values.empty()?0:std::accumulate(values.begin(),values.end(),0.0)/values.size(); }
};
struct Diagnostics {
  Ring intervals, render, present, pose_age_submission, pose_age_presentation, acquisition_age, uart_receive_age;
  std::uint64_t frames{}, missed{}, repeated{}, invalid{}, stale{}, torn{};
  std::uint64_t last_sequence{}, started{ns_now()};
  std::uint64_t last_model{}, last_reference{}, model_changes{}, reference_changes{}, producer_restarts{};
  std::string driver, renderer, mode, timing_limit{"SDL_RenderPresent return is observed; physical page-flip completion is not exposed by SDL2"};
  void export_json(const std::string& path) const {
    const auto target=std::filesystem::path(path); std::filesystem::create_directories(target.parent_path());
    const auto temporary=target.string()+".tmp"; std::ofstream out(temporary,std::ios::trunc);
    auto stats=[&](const Ring& r){std::ostringstream s;s<<"{\"samples\":"<<r.values.size()<<",\"mean\":"<<r.mean()<<",\"p95\":"<<r.percentile(.95)<<",\"p99\":"<<r.percentile(.99)<<",\"max\":"<<(r.values.empty()?0:*std::max_element(r.values.begin(),r.values.end()))<<"}";return s.str();};
    out<<std::fixed<<std::setprecision(4)<<"{\n  \"frames\":"<<frames<<",\n  \"elapsed_seconds\":"<<ms(ns_now()-started)/1000.0
       <<",\n  \"display_mode\":\""<<mode<<"\",\n  \"video_driver\":\""<<driver<<"\",\n  \"renderer\":\""<<renderer
       <<"\",\n  \"presentation_measurement_limit\":\""<<timing_limit<<"\",\n  \"pending_frames\":0,\n  \"missed_deadlines\":"<<missed
       <<",\n  \"repeated_pose_frames\":"<<repeated<<",\n  \"invalid_pose_frames\":"<<invalid<<",\n  \"stale_pose_frames\":"<<stale
       <<",\n  \"rejected_snapshots\":"<<torn<<",\n  \"frame_interval_ms\":"<<stats(intervals)<<",\n  \"render_ms\":"<<stats(render)
       <<",\n  \"present_call_ms\":"<<stats(present)<<",\n  \"pose_age_at_submission_ms\":"<<stats(pose_age_submission)
       <<",\n  \"pose_age_after_present_call_ms\":"<<stats(pose_age_presentation)
       <<",\n  \"acquisition_age_at_submission_ms\":"<<stats(acquisition_age)
       <<",\n  \"uart_receive_age_at_submission_ms\":"<<stats(uart_receive_age)
       <<",\n  \"model_generation_changes\":"<<model_changes<<",\n  \"reference_generation_changes\":"<<reference_changes
       <<",\n  \"producer_restarts\":"<<producer_restarts<<"\n}\n";
    out.close(); std::filesystem::rename(temporary,target);
  }
};

} // namespace

int main(int argc,char** argv) try {
  const auto opt=options(argc,argv);
  std::signal(SIGINT,signal_handler); std::signal(SIGTERM,signal_handler); std::signal(SIGUSR1,signal_handler);
  if(!std::getenv("SDL_VIDEODRIVER")) SDL_setenv("SDL_VIDEODRIVER","KMSDRM",0);
  SDL_SetHint(SDL_HINT_RENDER_VSYNC,"1");
  if(SDL_Init(SDL_INIT_VIDEO|SDL_INIT_TIMER))throw std::runtime_error(SDL_GetError());
  std::vector<kairix::DisplayMode> modes;
  for(int i=0;i<SDL_GetNumDisplayModes(0);++i){SDL_DisplayMode candidate{};if(!SDL_GetDisplayMode(0,i,&candidate))modes.push_back({candidate.w,candidate.h,candidate.refresh_rate,i});}
  const auto selected=kairix::select_exact_1080p50(modes);if(!selected)throw std::runtime_error("exact 1920x1080@50 display mode is unavailable");
  SDL_DisplayMode display{};if(SDL_GetDisplayMode(0,selected->index,&display))throw std::runtime_error(SDL_GetError());
  auto* window=SDL_CreateWindow("Kairix Native",SDL_WINDOWPOS_UNDEFINED,SDL_WINDOWPOS_UNDEFINED,1920,1080,SDL_WINDOW_FULLSCREEN|SDL_WINDOW_ALLOW_HIGHDPI);
  if(!window)throw std::runtime_error(SDL_GetError());
  if(SDL_SetWindowDisplayMode(window,&display))throw std::runtime_error(SDL_GetError());
  auto* renderer=SDL_CreateRenderer(window,-1,SDL_RENDERER_ACCELERATED|SDL_RENDERER_PRESENTVSYNC);
  if(!renderer)throw std::runtime_error(SDL_GetError());
  SDL_RendererInfo info{};SDL_GetRendererInfo(renderer,&info);
  Diagnostics diag;diag.driver=SDL_GetCurrentVideoDriver()?SDL_GetCurrentVideoDriver():"unknown";diag.renderer=info.name?info.name:"unknown";diag.mode="1920x1080@50";
  std::optional<kairix::PoseMmapReader> pose_reader;
  if(opt.mode!="internal"){pose_reader.emplace(opt.pose_path);pose_reader->open();}
  const auto start=Clock::now();std::uint64_t previous_frame=0;std::optional<kairix::Pose> held;
  while(running){
    SDL_Event event{};while(SDL_PollEvent(&event))if(event.type==SDL_QUIT)running=false;
    const auto frame_start=ns_now();if(previous_frame){const auto interval=ms(frame_start-previous_frame);diag.intervals.add(interval);if(interval>30)diag.missed++;}previous_frame=frame_start;
    std::optional<kairix::Pose> pose;std::string pose_error;
    if(pose_reader){
      pose=opt.mode=="latest"?pose_reader->latest(&pose_error):pose_reader->at_timestamp(frame_start-static_cast<std::uint64_t>(opt.interpolation_delay_ms*1e6));
      if(!pose){diag.torn++;}else{if(pose->sequence==diag.last_sequence)diag.repeated++;diag.last_sequence=pose->sequence;if(pose->stale)diag.stale++;if((pose->flags&3u)!=3u)diag.invalid++;if(diag.last_model&&diag.last_model!=pose->model_generation)diag.model_changes++;if(diag.last_reference&&diag.last_reference!=pose->reference_generation)diag.reference_changes++;if(pose->producer_restart)diag.producer_restarts++;diag.last_model=pose->model_generation;diag.last_reference=pose->reference_generation;if(!pose->stale&&(pose->flags&3u)==3u)held=pose;}
      if((!pose||pose->stale||(pose->flags&3u)!=3u)&&opt.stale_policy=="hold")pose=held;
      else if(!pose||pose->stale||(pose->flags&3u)!=3u)pose.reset();
    }
    SDL_SetRenderDrawColor(renderer,0,255,0,255);SDL_RenderClear(renderer);
    const double elapsed=std::chrono::duration<double>(Clock::now()-start).count();
    if(opt.mode=="internal"){
      const double offset=kairix::diagnostic_motion(opt.motion,elapsed);
      SDL_SetRenderDrawColor(renderer,255,255,255,255);
      if(opt.motion=="vertical")SDL_RenderDrawLine(renderer,0,static_cast<int>(540+offset),1919,static_cast<int>(540+offset));
      else SDL_RenderDrawLine(renderer,static_cast<int>(960+offset),0,static_cast<int>(960+offset),1079);
      SDL_Rect marker{static_cast<int>(950+offset),530,20,20};SDL_RenderDrawRect(renderer,&marker);
    }else if(pose){
      const auto a=kairix::project_point({-5,20,0},*pose,1920,1080),b=kairix::project_point({5,20,0},*pose,1920,1080);
      if(a&&b){SDL_SetRenderDrawColor(renderer,255,255,255,255);SDL_RenderDrawLine(renderer,static_cast<int>(a->x),static_cast<int>(a->y),static_cast<int>(b->x),static_cast<int>(b->y));}
      diag.pose_age_submission.add(ms(frame_start-pose->publication_ns));
      diag.acquisition_age.add(ms(frame_start-pose->acquisition_ns));if(pose->pico_receive_ns)diag.uart_receive_age.add(ms(frame_start-pose->pico_receive_ns));
    }
    const auto rendered=ns_now();diag.render.add(ms(rendered-frame_start));SDL_RenderPresent(renderer);const auto presented=ns_now();diag.present.add(ms(presented-rendered));
    if(pose)diag.pose_age_presentation.add(ms(presented-pose->publication_ns));diag.frames++;
    if(export_requested.exchange(false))diag.export_json(opt.diagnostics);
  }
  diag.export_json(opt.diagnostics);SDL_DestroyRenderer(renderer);SDL_DestroyWindow(window);SDL_Quit();return 0;
}catch(const std::exception& error){std::cerr<<"kairix-native-renderer: "<<error.what()<<"\n";return 1;}
