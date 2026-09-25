using Toy.Domain;
using Toy.Messaging;

namespace Toy.Web;

public static class Program
{
    public static void Main()
    {
        var order = new Order { Id = "A-1" };
        var publisher = new Publisher();
        if (order.Validate().Ok)
            publisher.Publish("orders", order.Id);
    }
}
