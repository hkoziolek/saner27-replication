using Toy.Common;

namespace Toy.Domain;

public sealed class Order
{
    public required string Id { get; init; }
    public Result Validate() => string.IsNullOrEmpty(Id) ? Result.Fail("missing id") : Result.Success();
}
