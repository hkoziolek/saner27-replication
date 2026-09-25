var builder = DistributedApplication.CreateBuilder(args);

var redis = builder.AddRedis("redis");

var basketApi = builder.AddProject<Projects.Basket_API>("basket-api")
    .WithReference(redis);

var catalogApi = builder.AddProject<Projects.Catalog_API>("catalog-api");

builder.AddProject<Projects.WebApp>("webapp")
    .WithReference(basketApi)
    .WithReference(catalogApi);

builder.Build().Run();
