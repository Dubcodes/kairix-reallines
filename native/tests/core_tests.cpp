#include "kairix_native.hpp"
#include <cassert>
#include <cmath>
#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

int main() {
  kairix::Pose camera{};
  camera.z = 1.7; camera.fov = 60.0;
  const auto centre = kairix::project_point({0, 10, 1.7}, camera, 1920, 1080);
  assert(centre && std::abs(centre->x - 960) < 1e-9 && std::abs(centre->y - 540) < 1e-9);
  const auto right = kairix::project_point({1, 10, 1.7}, camera, 1920, 1080);
  assert(right && right->x > centre->x);
  assert(kairix::diagnostic_motion("stationary", 99) == 0);
  assert(std::abs(kairix::diagnostic_motion("sine", 1, 10) - 10) < 1e-9);
  kairix::Pose a{}, b{}; a.publication_ns = 100; b.publication_ns = 200; a.pan = 0; b.pan = 10;
  assert(std::abs(kairix::interpolate_pose(a, b, 150).pan - 5) < 1e-9);
  assert(!kairix::select_exact_1080p50({{1920,1080,60,0},{1280,720,50,1}}));
  const auto exact=kairix::select_exact_1080p50({{1920,1080,60,0},{1920,1080,50,1}});
  assert(exact&&exact->index==1);
  std::ifstream fixture(KAIRIX_PROJECTION_FIXTURE);
  std::string row; std::getline(fixture,row);
  int fixtures=0;
  while(std::getline(fixture,row)){
    std::stringstream stream(row);std::string value;std::vector<std::string> fields;
    while(std::getline(stream,value,','))fields.push_back(value);
    assert(fields.size()==15);kairix::Pose c{};
    c.x=std::stod(fields[4]);c.y=std::stod(fields[5]);c.z=std::stod(fields[6]);c.pan=std::stod(fields[7]);c.tilt=std::stod(fields[8]);c.roll=std::stod(fields[9]);c.fov=std::stod(fields[10]);
    auto projected=kairix::project_point({std::stod(fields[1]),std::stod(fields[2]),std::stod(fields[3])},c,std::stod(fields[11]),std::stod(fields[12]));
    assert(projected);assert(std::abs(projected->x-std::stod(fields[13]))<1e-6);assert(std::abs(projected->y-std::stod(fields[14]))<1e-6);fixtures++;
  }
  assert(fixtures==3);
  std::cout << "native core tests passed\n";
}
