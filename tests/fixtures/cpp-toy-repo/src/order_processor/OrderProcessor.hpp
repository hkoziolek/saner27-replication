#pragma once

#include "messaging/Publisher.hpp"
#include "pricing/PriceCalculator.hpp"

namespace company::orders {

/// Consumes order events, validates and enriches them, and publishes results.
class OrderProcessor {
 public:
  /// Process one order line; returns whether it was accepted.
  bool process(int quantity, double unit_price);

 private:
  company::messaging::Publisher publisher_;
  company::pricing::PriceCalculator calculator_;
};

}  // namespace company::orders
