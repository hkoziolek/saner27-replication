#include "messaging/Publisher.hpp"

namespace company::messaging {

company::common::Result<bool> Publisher::publish(const std::string& topic,
                                                 const std::string& payload) {
  (void)topic;
  (void)payload;
  return company::common::Result<bool>::success(true);
}

}  // namespace company::messaging
