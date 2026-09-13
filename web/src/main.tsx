import React from "react";
import ReactDOM from "react-dom/client";

// 自托管 Inter（variable，随构建产物一起发出，不依赖外部字体 CDN）
import "@fontsource-variable/inter/wght.css";

import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
