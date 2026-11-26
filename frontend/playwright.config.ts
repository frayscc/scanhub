import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
 testDir:'./e2e', fullyParallel:false, workers:1,
 use:{baseURL:'http://127.0.0.1:18767', channel:'msedge', trace:'retain-on-failure'},
 projects:[{name:'desktop',use:{...devices['Desktop Edge'],viewport:{width:1440,height:1000}}},{name:'mobile',use:{...devices['iPhone 13'],defaultBrowserType:'chromium'}}],
 webServer:{command:(process.platform==='win32'?'..\\.venv\\Scripts\\python':'../.venv/bin/python')+' ../scripts/e2e_server.py',url:'http://127.0.0.1:18767/api/health',reuseExistingServer:false,timeout:30000}
})
