cmake_minimum_required(VERSION 3.16)
project(InteropMixed CXX)

set(CMAKE_CXX_STANDARD 17)

# The native library that the C# side reaches via [DllImport("NativeMath")]. Building it as a
# real CMake SHARED target means the C++ extractor produces cpp:target:NativeMath, which the
# interop resolver (§16#15) binds the P/Invoke edge to.
add_library(NativeMath SHARED src/native/NativeMath.cpp)
target_include_directories(NativeMath PUBLIC src/native)
