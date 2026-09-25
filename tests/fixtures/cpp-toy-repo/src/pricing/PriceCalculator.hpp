#pragma once

#include "common/Result.hpp"

namespace company::pricing {

/// Computes prices for orders.
class PriceCalculator {
 public:
  /// Compute the total price for a quantity at a unit price.
  company::common::Result<double> total(int quantity, double unit_price);
};

}  // namespace company::pricing
