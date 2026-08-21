const { withAndroidManifest, withDangerousMod } = require("@expo/config-plugins");
const fs = require("fs");
const path = require("path");

const NETWORK_XML = `<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
  <base-config cleartextTrafficPermitted="true">
    <trust-anchors>
      <certificates src="system"/>
      <certificates src="@raw/byoi_ca"/>
    </trust-anchors>
  </base-config>
</network-security-config>
`;

function withByoiCa(config) {
  config = withDangerousMod(config, [
    "android",
    async (cfg) => {
      const caSrc = path.join(cfg.modRequest.projectRoot, "assets", "ca.pem");
      if (!fs.existsSync(caSrc)) {
        return cfg;
      }
      const root = cfg.modRequest.platformProjectRoot;
      const rawDir = path.join(root, "app/src/main/res/raw");
      const xmlDir = path.join(root, "app/src/main/res/xml");
      fs.mkdirSync(rawDir, { recursive: true });
      fs.mkdirSync(xmlDir, { recursive: true });
      fs.copyFileSync(caSrc, path.join(rawDir, "byoi_ca.pem"));
      fs.writeFileSync(path.join(xmlDir, "network_security_config.xml"), NETWORK_XML);
      return cfg;
    },
  ]);
  config = withAndroidManifest(config, (cfg) => {
    const caSrc = path.join(cfg.modRequest.projectRoot, "assets", "ca.pem");
    if (!fs.existsSync(caSrc)) {
      return cfg;
    }
    const app = cfg.modResults.manifest.application[0];
    app.$["android:networkSecurityConfig"] = "@xml/network_security_config";
    return cfg;
  });
  return config;
}

module.exports = withByoiCa;
