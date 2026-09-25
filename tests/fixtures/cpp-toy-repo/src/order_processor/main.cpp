#include "order_processor/OrderProcessor.hpp"

int main() {
  company::orders::OrderProcessor processor;
  return processor.process(3, 9.99) ? 0 : 1;
}
