function authorize(path: string): string {
  return `authorized:${path}`;
}

function audit(path: string): string {
  return `audited:${path}`;
}

export function handleRequest(path: string): string {
  return `${authorize(path)}|${audit(path)}`;
}
