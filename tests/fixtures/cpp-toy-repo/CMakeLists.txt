cmake_minimum_required(VERSION 3.16)
project(CppToy CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Header-only INTERFACE library (§4.1 caveat: no own link edge; surfaces via consumers).
add_library(common INTERFACE)
target_include_directories(common INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include)

# Static libraries (namespaces company::messaging / company::pricing).
add_library(messaging STATIC src/messaging/Publisher.cpp)
target_include_directories(messaging PUBLIC src/messaging ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(messaging PUBLIC common)

add_library(pricing STATIC src/pricing/PriceCalculator.cpp)
target_include_directories(pricing PUBLIC src/pricing ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(pricing PUBLIC common)

# Executable that links the two libs PRIVATE (a private-visibility edge — §6.2/§7.4).
add_executable(order_processor
  src/order_processor/main.cpp
  src/order_processor/OrderProcessor.cpp)
target_include_directories(order_processor PRIVATE src/order_processor)
target_link_libraries(order_processor PRIVATE messaging pricing)

# A test executable (excluded by curation globs later, not by the extractor — §4.2/§7.4).
enable_testing()
add_executable(order_tests tests/order_tests.cpp)
target_link_libraries(order_tests PRIVATE pricing)
add_test(NAME order_tests COMMAND order_tests)
