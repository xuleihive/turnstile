type ReportEntry = {
  id: string
  title: string
  description: string
  position: number
}

export function reportDirectory<T extends ReportEntry>(reports: readonly T[], search = ""): T[] {
  const term = search.trim().toLocaleLowerCase()
  return reports.filter((report) =>
    !term || `${report.title}\n${report.description}`.toLocaleLowerCase().includes(term),
  ).sort((a, b) => a.position - b.position || a.id.localeCompare(b.id))
}

export function selectedReport<T extends ReportEntry>(
  reports: readonly T[],
  requestedId: string | null,
): T | undefined {
  // An explicit old link must not silently display a different report.
  return requestedId === null ? reports[0] : reports.find((report) => report.id === requestedId)
}

export function reportCenterUrl(href: string, reportId: string | null): URL {
  const url = new URL(href)
  url.searchParams.set("page", "pinned-report")
  if (reportId === null) url.searchParams.delete("chart")
  else url.searchParams.set("chart", reportId)
  return url
}
