#pragma once

namespace company::nativemath {

/// Adds two integers (exported for P/Invoke from the managed side).
extern "C" int native_add(int a, int b);

}  // namespace company::nativemath
