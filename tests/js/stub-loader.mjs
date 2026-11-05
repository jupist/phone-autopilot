// Node resolve hook for the smoke test. Maps the three specifiers the desktop
// loader allows onto stubs, and refuses any other import from desktop/plugin.js
// (the same allowlist the real loader enforces).
const STUBS = {
  '@hermes/plugin-sdk': './stubs/plugin-sdk.mjs',
  react: './stubs/react.mjs',
  'react/jsx-runtime': './stubs/jsx-runtime.mjs',
}

export async function resolve(specifier, context, nextResolve) {
  if (Object.prototype.hasOwnProperty.call(STUBS, specifier)) {
    return { url: new URL(STUBS[specifier], import.meta.url).href, shortCircuit: true }
  }
  if (context.parentURL?.endsWith('/desktop/plugin.js')) {
    throw new Error(`plugin.js may not import "${specifier}"`)
  }
  return nextResolve(specifier, context)
}
