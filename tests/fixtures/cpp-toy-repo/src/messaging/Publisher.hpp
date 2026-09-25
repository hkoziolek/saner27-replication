#pragma once

#include <string>

#include "common/Result.hpp"

namespace company::messaging {

/// Publishes order events to a message bus.
class Publisher {
 public:
  /// Publish a single event payload; returns whether the broker accepted it.
  company::common::Result<bool> publish(const std::string& topic,
                                        const std::string& payload);
};

}  // namespace company::messaging
