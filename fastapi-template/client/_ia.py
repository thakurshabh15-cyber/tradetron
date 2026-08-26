p = 'src/App.jsx'
s = open(p, encoding='utf-8').read()
anchor_imp = 'const BacktestLab = lazy(() => import("./pages/BacktestLab"));'
assert anchor_imp in s, 'lazy anchor missing'
if 'pages/Markets' not in s:
    add = anchor_imp + '\n' + '\n'.join([
        'const Markets = lazy(() => import("./pages/Markets"));',
        'const MarketDetail = lazy(() => import("./pages/MarketDetail"));',
        'const PortfolioPage = lazy(() => import("./pages/Portfolio"));',
        'const ExecutionPage = lazy(() => import("./pages/Execution"));',
        'const KYC = lazy(() => import("./pages/KYC"));',
        'const TradeJournal = lazy(() => import("./pages/TradeJournal"));',
    ])
    s = s.replace(anchor_imp, add, 1)
r_anchor = '<Route path="/backtest" element={<BacktestLab />} />'
assert r_anchor in s, 'route anchor missing'
if '/command-center' not in s:
    add_r = '\n'.join([
        '                <Route path="/command-center" element={<Dashboard />} />',
        '                <Route path="/markets" element={<Markets />} />',
        '                <Route path="/markets/:symbol" element={<MarketDetail />} />',
        '                <Route path="/portfolio" element={<ProtectedRoute><PortfolioPage /></ProtectedRoute>} />',
        '                <Route path="/execution" element={<ProtectedRoute><ExecutionPage /></ProtectedRoute>} />',
        '                <Route path="/risk-center" element={<ProtectedRoute><ExecutionPage /></ProtectedRoute>} />',
        '                <Route path="/kyc" element={<KYC />} />',
        '                <Route path="/trade-journal" element={<ProtectedRoute><TradeJournal /></ProtectedRoute>} />',
        '                <Route path="/reality-mode" element={<BacktestLab />} />',
    ])
    s = s.replace(r_anchor, r_anchor + '\n' + add_r, 1)
open(p, 'w', encoding='utf-8', newline='').write(s)
print('App.jsx patched')

p2 = 'src/components/Sidebar.jsx'
s2 = open(p2, encoding='utf-8').read()
old_icons = '  LineChart,\n} from "lucide-react";'
if 'Wallet,' not in s2.split('from "lucide-react"')[0]:
    assert old_icons in s2, 'icon block missing'
    s2 = s2.replace(old_icons, '  LineChart,\n  Wallet,\n  Radar,\n  FileText,\n  BadgeCheck,\n  Globe2,\n} from "lucide-react";', 1)
start = s2.index('const NAV_GROUPS = [')
end = s2.index('// Flat render list')
new_groups = '''const NAV_GROUPS = [
  { header: "COMMAND", items: [
    { to: "/", icon: LayoutDashboard, label: "Command Center" },
    { to: "/markets", icon: Globe2, label: "Markets" },
    { to: "/portfolio", icon: Wallet, label: "Portfolio" },
    { to: "/execution", icon: Radar, label: "Live Execution" },
  ] },
  { header: "BUILD", items: [
    { to: "/strategies", icon: Brain, label: "Strategies" },
    { to: "/quant-lab", icon: FlaskConical, label: "AI Strategy Builder" },
    { to: "/visual-builder", icon: Workflow, label: "Visual Options Builder" },
  ] },
  { header: "VALIDATE", items: [
    { to: "/backtest", icon: LineChart, label: "Truthful Backtest" },
    { to: "/reality-mode", icon: Activity, label: "Reality Mode" },
  ] },
  { header: "DISCOVER", items: [
    { to: "/marketplace", icon: Store, label: "Marketplace" },
    { to: "/copy-trading", icon: Users, label: "Copy Trading" },
  ] },
  { header: "CONTROL", items: [
    { to: "/execution#risk", icon: ShieldCheck, label: "Risk Center" },
    { to: "/broker-sessions", icon: Server, label: "Broker Sessions" },
    { to: "/watchlist", icon: Eye, label: "Watchlist & Alerts" },
  ] },
  { header: "ANALYZE", items: [
    { to: "/history", icon: History, label: "Trade History" },
    { to: "/trade-journal", icon: FileText, label: "AI Trade Journal" },
  ] },
  { header: "SYSTEM", items: [
    { to: "/pricing", icon: CreditCard, label: "Pricing & Plans" },
    { to: "/kyc", icon: BadgeCheck, label: "KYC Center" },
    { to: "/settings", icon: SettingsIcon, label: "Profile & Settings" },
    { to: "/admin", icon: Lock, label: "Admin Sentinel" },
  ] },
];

'''
s2 = s2[:start] + new_groups + s2[end:]
if '  Lock,' not in s2.split('from "lucide-react"')[0]:
    s2 = s2.replace('  CreditCard,', '  CreditCard,\n  Lock,', 1)
open(p2, 'w', encoding='utf-8', newline='').write(s2)
print('Sidebar expanded')
