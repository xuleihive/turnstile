import { pictureBridge } from "../client_user_plugin/images.mjs";
export { imagePluginEnabled, localMagpieURL } from "../client_user_plugin/images.mjs";
if (import.meta.main) await pictureBridge(process.argv[2], process.argv[3]);
