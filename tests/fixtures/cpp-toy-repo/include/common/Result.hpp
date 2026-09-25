#pragma once

/// \file
/// Shared result type used across the toy system.

namespace company::common {

/// A minimal success/failure result carrier (header-only contract).
template <class T>
struct Result {
  bool ok = false;
  T value{};

  static Result success(T v) { return Result{true, v}; }
  static Result failure() { return Result{false, T{}}; }
};

}  // namespace company::common
