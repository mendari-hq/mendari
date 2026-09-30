import type { NextConfig } from "next";

// Every page reads the registry at request time (REGISTRY_URL), so nothing is prerendered.
const config: NextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
};

export default config;
