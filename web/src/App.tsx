import { useEffect, useState } from "react"
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom"
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query"
import { apiFetch, getStoredToken, setStoredToken } from "@/lib/api"
import { AppProvider } from "@/lib/app-context"
import { AppShell } from "@/components/layout/AppShell"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { AssistantPage } from "@/pages/AssistantPage"
import { BriefingPage } from "@/pages/BriefingPage"
import { ResearchPage } from "@/pages/ResearchPage"
import { SettingsPage } from "@/pages/SettingsPage"
import { TrendsPage } from "@/pages/TrendsPage"
import { WorkbenchPage } from "@/pages/WorkbenchPage"

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
})

function AuthGate({ children }: { children: React.ReactNode }) {
  const [tokenInput, setTokenInput] = useState("")
  const [authError, setAuthError] = useState("")
  const [verified, setVerified] = useState(false)

  const statusQuery = useQuery({
    queryKey: ["auth-status"],
    queryFn: () => apiFetch<{ auth_required: boolean; loopback_bypass: boolean }>("/api/auth/status"),
  })

  useEffect(() => {
    const info = statusQuery.data
    if (!info) return
    if (info.loopback_bypass || !info.auth_required) {
      setVerified(true)
      return
    }
    if (getStoredToken()) {
      apiFetch("/api/topics")
        .then(() => setVerified(true))
        .catch(() => {
          setStoredToken("")
          setVerified(false)
        })
    }
  }, [statusQuery.data])

  if (statusQuery.isLoading) {
    return (
      <div className="flex h-screen items-center justify-center text-xs text-[var(--muted-foreground)]">
        正在连接 ResearchRadar 服务…
      </div>
    )
  }

  if (statusQuery.data?.auth_required && !statusQuery.data?.loopback_bypass && !verified) {
    return (
      <div className="flex h-screen items-center justify-center bg-[var(--background)] p-4">
        <div className="w-full max-w-sm rounded-lg border border-[var(--border)] bg-[var(--surface)] p-5 space-y-3 text-xs">
          <div className="text-sm font-semibold">需要 API 访问凭据</div>
          <p className="text-[var(--muted-foreground)]">
            当前服务端配置了 `RADAR_API_TOKEN` 或运行于非回环地址，请输入 Bearer Token 继续访问。
          </p>
          <Input
            type="password"
            placeholder="输入 RADAR_API_TOKEN"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
          />
          {authError && <div className="text-[var(--destructive)]">{authError}</div>}
          <Button
            className="w-full"
            onClick={async () => {
              setStoredToken(tokenInput)
              try {
                await apiFetch("/api/topics")
                setVerified(true)
                setAuthError("")
              } catch {
                setStoredToken("")
                setAuthError("Token 验证失败，请检查后重试")
              }
            }}
          >
            验证并进入
          </Button>
        </div>
      </div>
    )
  }

  return <>{children}</>
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthGate>
        <AppProvider>
          <BrowserRouter>
            <AppShell>
              <Routes>
                <Route path="/briefing" element={<BriefingPage />} />
                <Route path="/workbench" element={<WorkbenchPage />} />
                <Route path="/trends" element={<TrendsPage />} />
                <Route path="/research" element={<ResearchPage />} />
                <Route path="/assistant" element={<AssistantPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="*" element={<Navigate to="/briefing" replace />} />
              </Routes>
            </AppShell>
          </BrowserRouter>
        </AppProvider>
      </AuthGate>
    </QueryClientProvider>
  )
}
