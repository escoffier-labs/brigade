import { routeRequest } from "./router";

export function testRouteRequest(): void {
  if (!routeRequest("/health").includes("authorized")) {
    throw new Error("route was not authorized");
  }
}
