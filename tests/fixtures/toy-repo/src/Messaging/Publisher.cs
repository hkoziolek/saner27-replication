using Toy.Common;

namespace Toy.Messaging;

public sealed class Publisher
{
    public Result Publish(string topic, string payload) =>
        string.IsNullOrEmpty(topic) ? Result.Fail("no topic") : Result.Success();
}
