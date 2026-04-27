/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_CLERK_PUBLISHABLE_KEY: string;
  readonly VITE_JWT_TEMPLATE?: string;
  readonly VITE_ALLOWED_REDIRECT_HOSTS?: string;
  readonly VITE_ALLOWED_REDIRECT_PORTS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
