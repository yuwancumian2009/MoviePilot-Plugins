import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

// MoviePilot V3 联邦插件构建配置。
// 与宿主 MoviePilot-Frontend(v3) 对齐：Vite 5 / @originjs/vite-plugin-federation 1.4.x /
// vue 3.5.x / vuetify 3.7.3（宿主固定版本）；shared 一律 generate: false，复用宿主实例。
export default defineConfig({
  plugins: [
    vue(),
    federation({
      name: 'MessageRouter',
      filename: 'remoteEntry.js',
      exposes: {
        './Page': './src/components/Page.vue',
        './Config': './src/components/Config.vue',
      },
      shared: {
        vue: {
          requiredVersion: false,
          generate: false,
          singleton: true,
        },
        vuetify: {
          requiredVersion: false,
          generate: false,
          singleton: true,
        },
        'vuetify/styles': {
          requiredVersion: false,
          generate: false,
          singleton: true,
        },
      },
      format: 'esm',
    }),
  ],
  build: {
    // 联邦运行时使用顶层 await，构建目标必须为 esnext。
    target: 'esnext',
    minify: false,
    // V3 要求按组件拆分样式，避免整包样式塞进宿主页面。
    cssCodeSplit: true,
  },
  css: {
    preprocessorOptions: {
      scss: {
        additionalData: '/* messagerouter styles */',
      },
    },
    postcss: {
      plugins: [
        {
          postcssPlugin: 'internal:charset-removal',
          AtRule: {
            charset: (atRule) => {
              if (atRule.name === 'charset') {
                atRule.remove()
              }
            },
          },
        },
        {
          // V3 强制要求：联邦组件不得打包 Vuetify / MDI 全局基础样式，
          // 否则 html/body/:root/.v-*/.mdi-* 规则会污染宿主界面。
          postcssPlugin: 'vuetify-filter',
          Root(root) {
            const sourcePath = root.source?.input?.file?.replaceAll('\\', '/') || ''
            if (
              sourcePath.includes('/node_modules/vuetify/') ||
              sourcePath.includes('/node_modules/@mdi/')
            ) {
              root.nodes = []
              return
            }
            root.walkRules((rule) => {
              if (rule.selector && (rule.selector.includes('.v-') || rule.selector.includes('.mdi-'))) {
                rule.remove()
              }
            })
          },
        },
      ],
    },
  },
  server: {
    port: 5003,
    cors: true,
    origin: 'http://localhost:5003',
  },
})
