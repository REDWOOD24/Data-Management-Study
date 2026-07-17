# Set up the project.
cmake_minimum_required(VERSION 3.12)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
project( "DataManagementPlugin" )

# Disable annoying warnings
add_definitions("-DBOOST_ALLOW_DEPRECATED_HEADERS")
set(CMAKE_MODULE_PATH ${CMAKE_MODULE_PATH} "${CMAKE_SOURCE_DIR}/CMakeModules/")

# Find and set the source file.
include_directories(include/)
file(GLOB SOURCES src/*.cpp)

# Set up the library.
add_library(DataManagementPlugin SHARED ${SOURCES})

# Find SimGrid, CGSim, Boost, and FSMod
find_package(SimGrid REQUIRED)
find_package(Boost REQUIRED)
find_package(CGSim REQUIRED)
find_package(SQLite3   REQUIRED)


#Link libraries and Include Directories
target_link_libraries ( DataManagementPlugin PUBLIC ${SimGrid_LIBRARY} ${Boost_LIBRARIES}  CGSim::CGSim SQLite::SQLite3)
target_include_directories( DataManagementPlugin PUBLIC ${SimGrid_INCLUDE_DIR} ${CGSim_INCLUDE_DIR} ${Boost_INCLUDE_DIR})

# Prefer in-tree CGSim headers/libs during development (ahead of any installed copy).
set(_CGSIM_IN_TREE "${CMAKE_CURRENT_SOURCE_DIR}/../../CGSim")
if(EXISTS "${_CGSIM_IN_TREE}/include/file_manager.h")
  target_include_directories(DataManagementPlugin BEFORE PRIVATE "${_CGSIM_IN_TREE}/include")
endif()
if(EXISTS "${_CGSIM_IN_TREE}/build/libCGSim.dylib")
  target_link_libraries(DataManagementPlugin PUBLIC "${_CGSIM_IN_TREE}/build/libCGSim.dylib")
elseif(EXISTS "${_CGSIM_IN_TREE}/build/libCGSim.so")
  target_link_libraries(DataManagementPlugin PUBLIC "${_CGSIM_IN_TREE}/build/libCGSim.so")
endif()


