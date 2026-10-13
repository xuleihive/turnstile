export function visibleApimPages<T extends { id: string }>(
  pages: readonly T[],
  permissions: readonly string[],
): T[]

export function resolveApimPage(page: string, permissions: readonly string[]): string
