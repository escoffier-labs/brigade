import { handleRequest } from "./service";

export function routeRequest(path: string): string {
  return handleRequest(path);
}
