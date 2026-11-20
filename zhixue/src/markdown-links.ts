export function workspaceMarkdownRoute(href: string | undefined) {
  if (!href) return undefined;
  if (href.startsWith('#/library?file=') || href.startsWith('#/library?wiki=')) return href.slice(1);
  if (/^#\/lessons\/[a-f0-9-]{36}\?readingVersion=[1-9]\d*(?:&section=\d+&selectionStart=\d+&selectionEnd=[1-9]\d*)?$/.test(href)) {
    return href.slice(1);
  }
  return undefined;
}

export function isCourseSourceRoute(route: string) {
  return route.startsWith('/lessons/') || /^\/library\?file=course-[a-f0-9-]{36}&at=[1-9]\d*&revision=[a-f0-9]{64}$/.test(route);
}
