import { createRequire } from "node:module"
import { fileURLToPath } from "node:url"

const frontendRequire = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = await import(frontendRequire.resolve("rolldown"))

export async function loadTypeScriptModule(url) {
  const bundle = await rolldown({ input: fileURLToPath(url) })
  try {
    const { code } = (await bundle.generate({ format: "esm" })).output[0]
    return await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`)
  } finally {
    await bundle.close()
  }
}
