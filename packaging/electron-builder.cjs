const path = require('node:path');

// This configuration builds an unsigned, isolated AIK-002 development spike.
// A successful build must never be interpreted as the AIK-064 release gate.
module.exports = {
  appId: 'io.github.jaeseongs95.aik.foundation-spike',
  productName: 'AIK Foundation Spike',
  executableName: 'AIK Foundation Spike',
  asar: true,
  directories: {
    output: path.join(__dirname, 'dist'),
    buildResources: path.join(__dirname, 'resources'),
  },
  files: [
    'dist/**',
    'electron/**',
    'package.json',
    '!**/*.map',
  ],
  extraResources: [
    {
      from: path.join(__dirname, 'build', 'engine', 'aik-engine'),
      to: 'engine',
      filter: ['**/*'],
    },
  ],
  forceCodeSigning: false,
  publish: null,
  win: {
    target: [{ target: 'nsis', arch: ['x64'] }],
    executableName: 'AIK Foundation Spike',
    // Keep the spike's executable metadata, but do not discover or use a
    // publisher certificate for this explicitly unsigned development artifact.
    signAndEditExecutable: true,
    signExecutable: false,
    requestedExecutionLevel: 'asInvoker',
  },
  nsis: {
    oneClick: true,
    perMachine: false,
    allowElevation: false,
    packElevateHelper: false,
    deleteAppDataOnUninstall: false,
    runAfterFinish: false,
    createDesktopShortcut: false,
    createStartMenuShortcut: true,
    shortcutName: 'AIK Foundation Spike (Development)',
    artifactName: 'AIK-Foundation-Spike-${version}-win-x64-dev-unsigned.${ext}',
  },
};
