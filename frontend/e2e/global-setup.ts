import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const projectRoot = fileURLToPath(new URL("..", import.meta.url));
const astroCli = fileURLToPath(new URL("../node_modules/astro/bin/astro.mjs", import.meta.url));

function astro(...args: string[]): number {
  return spawnSync(process.execPath, [astroCli, ...args], {
    cwd: projectRoot,
    stdio: "inherit",
  }).status ?? 1;
}

export default function globalSetup(): () => void {
  astro("dev", "stop");
  const status = astro("dev", "--background", "--host", "127.0.0.1", "--port", "3000");
  if (status !== 0) throw new Error("Não foi possível iniciar o servidor Astro para o E2E.");
  return () => {
    astro("dev", "stop");
  };
}
