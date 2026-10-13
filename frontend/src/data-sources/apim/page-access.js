/**
 * @param {readonly {id: string}[]} pages
 * @param {readonly string[]} permissions
 */
export function visibleApimPages(pages, permissions) {
  return pages.filter((page) => page.id !== "budgets" || permissions.includes("usage.read.all"))
}

/** @param {string} page @param {readonly string[]} permissions */
export function resolveApimPage(page, permissions) {
  return page === "budgets" && !permissions.includes("usage.read.all")
    ? "finops-overview"
    : page
}
