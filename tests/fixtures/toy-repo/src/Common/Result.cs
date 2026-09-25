namespace Toy.Common;

public sealed record Result(bool Ok, string? Error = null)
{
    public static Result Success() => new(true);
    public static Result Fail(string error) => new(false, error);
}
