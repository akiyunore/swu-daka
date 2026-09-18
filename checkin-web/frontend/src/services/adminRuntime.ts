function metaValue(name: string): string | null {
  return document.querySelector<HTMLMetaElement>(`meta[name="${name}"]`)?.content ?? null;
}

export function adminPagePath(): string | null {
  return metaValue("swu-admin-page-path") ?? (import.meta.env.DEV ? import.meta.env.VITE_ADMIN_PAGE_PATH ?? null : null);
}

export function adminApiPrefix(): string | null {
  return metaValue("swu-admin-api-prefix") ?? (import.meta.env.DEV ? import.meta.env.VITE_ADMIN_API_PREFIX ?? null : null);
}

export function adminApi(suffix: string): string {
  const prefix = adminApiPrefix();
  if (!prefix || !suffix.startsWith("/")) throw new Error("管理员接口配置缺失");
  return `${prefix}${suffix}`;
}
