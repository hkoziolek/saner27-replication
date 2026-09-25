#include "order_processor/OrderProcessor.hpp"

namespace company::orders {

bool OrderProcessor::process(int quantity, double unit_price) {
  auto price = calculator_.total(quantity, unit_price);
  if (!price.ok) {
    return false;
  }
  auto published = publisher_.publish("orders", "processed");
  return published.ok;
}

}  // namespace company::orders
