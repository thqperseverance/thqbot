import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 产物直接落到 gateway 的静态目录，这样容器镜像里自带前端，无需额外 web 服务。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../services/gateway/web",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8090",
        changeOrigin: true,
      },
      "/health": {
        target: "http://127.0.0.1:8090",
        changeOrigin: true,
      },
    },
  },
});
