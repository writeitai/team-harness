export type NavItem = {
  title: string;
  href: string;
  children?: NavItem[];
};

// Single source of truth for the docs sidebar order and grouping. It also
// drives prev/next pagination. Add a page here when you add its page.mdx.
export const docsNavigation: NavItem[] = [
  { title: "Introduction", href: "/docs" },
  { title: "Getting Started", href: "/docs/getting-started" },
  { title: "Concepts", href: "/docs/concepts" },
  { title: "Configuration", href: "/docs/configuration" },
  { title: "Workers", href: "/docs/workers" },
  { title: "Providers & Auth", href: "/docs/providers" },
  { title: "Skills", href: "/docs/skills" },
  { title: "Context Management", href: "/docs/context-management" },
  { title: "Python SDK", href: "/docs/sdk" },
  {
    title: "Reference",
    href: "/docs/cli-reference",
    children: [
      { title: "CLI Reference", href: "/docs/cli-reference" },
      { title: "Coordinator Tools", href: "/docs/coordinator-tools" },
      { title: "Run Logs", href: "/docs/run-logs" },
    ],
  },
  { title: "Troubleshooting", href: "/docs/troubleshooting" },
];

export function flattenNavigation(items: NavItem[]): NavItem[] {
  const result: NavItem[] = [];
  for (const item of items) {
    result.push(item);
    if (item.children) {
      result.push(...flattenNavigation(item.children));
    }
  }
  return result;
}

export function findAdjacentPages(pathname: string): {
  prev: NavItem | null;
  next: NavItem | null;
} {
  // De-duplicate on href so a section header that points at its first child
  // (e.g. "Reference" -> Session Layout) does not create a self-adjacency.
  const seen = new Set<string>();
  const flat = flattenNavigation(docsNavigation).filter((item) => {
    if (seen.has(item.href)) return false;
    seen.add(item.href);
    return true;
  });

  const normalize = (p: string) => (p.length > 1 ? p.replace(/\/$/, "") : p);
  const target = normalize(pathname);
  const index = flat.findIndex((item) => normalize(item.href) === target);
  if (index === -1) {
    return { prev: null, next: null };
  }
  return {
    prev: index > 0 ? flat[index - 1] : null,
    next: index < flat.length - 1 ? flat[index + 1] : null,
  };
}
