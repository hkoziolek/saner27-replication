#include "pricing/PriceCalculator.hpp"

namespace company::pricing {

company::common::Result<double> PriceCalculator::total(int quantity, double unit_price) {
  if (quantity < 0) {
    return company::common::Result<double>::failure();
  }
  return company::common::Result<double>::success(quantity * unit_price);
}

}  // namespace company::pricing
