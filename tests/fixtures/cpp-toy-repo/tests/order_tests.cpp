#include "pricing/PriceCalculator.hpp"

// Minimal hand-rolled test (no framework dependency) — exercises the pricing lib.
int main() {
  company::pricing::PriceCalculator calc;
  auto r = calc.total(2, 5.0);
  return (r.ok && r.value == 10.0) ? 0 : 1;
}
