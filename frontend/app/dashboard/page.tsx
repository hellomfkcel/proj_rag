"use client";
// Dashboard — Phase 6 with Recharts

import { useEffect, useState } from "react";
import { useAuthStore } from "@/stores/useAuthStore";
import Header from "@/app/components/Header";
import { usageStats, topKBs, docStats, qualityStats } from "@/lib/dashboard";
import { getAppConfig } from "@/lib/settings";
import { isAdmin } from "@/lib/permissions";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, Legend,
} from "recharts";

interface BarItem { date?:string; kb_id?:string; status?:string; count:number; label?:string; }
interface QualityData { context_recall:number; faithfulness:number; eval_set_size?:number; valid_evaluations?:number; }

const COLORS = ["#3b82f6", "#f59e0b", "#ef4444", "#22c55e", "#8b5cf6", "#6b7280"];

export default function DashboardPage() {
  const token=useAuthStore(s=>s.token);

  const [usage,setUsage]=useState<BarItem[]>([]);
  const [kbs,setKBs]=useState<BarItem[]>([]);
  const [docs,setDocs]=useState<BarItem[]>([]);
  const [quality,setQuality]=useState<QualityData|null>(null);
  const [appCfg,setAppCfg]=useState<{grafana_url:string;langfuse_url:string;cerbos_url:string;admin_console_url:string}|null>(null);

  useEffect(()=>{if(!token)return;loadAll();},[token]);

  const loadAll=async()=>{
    try{setUsage(await usageStats(7));}catch{}
    try{setKBs(await topKBs(7));}catch{}
    try{setDocs(await docStats());}catch{}
    try{setQuality(await qualityStats());}catch{}
    getAppConfig().then(setAppCfg).catch(()=>{});
  };

  // Format data for charts
  const usageData = usage.map(d => ({ name: (d.date||"").slice(5)||d.label||"", 查询量: d.count }));
  const kbData = kbs.map(d => ({ name: (d.kb_id||d.label||"Unknown").slice(0,8), 查询次数: d.count }));
  const docData = docs.map(d => ({ name: d.status||"unknown", value: d.count }));
  const qualityItems = quality ? [
    { name: "Context Recall", value: quality.context_recall*100, color: "#22c55e" },
    { name: "Faithfulness", value: quality.faithfulness*100, color: "#3b82f6" },
  ] : [];

  if (!token) return null;

  if (!isAdmin()) {
    return <div className="min-h-screen bg-gray-50">
      <Header />
      <main className="max-w-4xl mx-auto px-6 py-8">
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-12 text-center">
          <div className="text-6xl mb-4">🔒</div>
          <h1 className="text-2xl font-bold text-gray-900 mb-2">需要管理员权限</h1>
          <p className="text-gray-500 mb-6">
            Dashboard 和系统运行数据仅对系统管理员开放。
          </p>
          <a href="/kb" className="inline-block px-6 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition font-medium">
            返回知识库 →
          </a>
        </div>
      </main>
    </div>;
  }

  return <div className="min-h-screen bg-gray-50">
    <Header />
    <main className="max-w-6xl mx-auto px-6 py-8 space-y-8">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">📊 Dashboard</h1>
        <span className="text-xs text-gray-400">数据来自 audit_logs · 近 7 天</span>
      </div>

      {/* Usage bar chart */}
      <section className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
        <h2 className="text-lg font-semibold mb-4">📈 查询量趋势</h2>
        {usageData.length===0 ? <p className="text-gray-400 text-sm">暂无数据</p> : (
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={usageData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0"/>
              <XAxis dataKey="name" tick={{fontSize:12}}/>
              <YAxis tick={{fontSize:12}} allowDecimals={false}/>
              <Tooltip/>
              <Bar dataKey="查询量" fill="#3b82f6" radius={[4,4,0,0]}/>
            </BarChart>
          </ResponsiveContainer>
        )}
      </section>

      <div className="grid grid-cols-2 gap-6">
        {/* Top KBs */}
        <section className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
          <h2 className="text-lg font-semibold mb-4">🏆 活跃知识库 Top 10</h2>
          {kbData.length===0 ? <p className="text-gray-400 text-sm">暂无数据</p> : (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={kbData.slice(0,10)} layout="vertical">
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0"/>
                <XAxis type="number" tick={{fontSize:12}} allowDecimals={false}/>
                <YAxis type="category" dataKey="name" width={80} tick={{fontSize:11}}/>
                <Tooltip/>
                <Bar dataKey="查询次数" fill="#8b5cf6" radius={[0,4,4,0]}/>
              </BarChart>
            </ResponsiveContainer>
          )}
        </section>

        {/* Doc status pie chart */}
        <section className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
          <h2 className="text-lg font-semibold mb-4">📄 文档处理状态</h2>
          {docData.length===0 ? <p className="text-gray-400 text-sm">暂无数据</p> : (
            <ResponsiveContainer width="100%" height={250}>
              <PieChart>
                <Pie data={docData} cx="50%" cy="50%" outerRadius={90} dataKey="value" label={({name,value})=>`${name}: ${value}`}>
                  {docData.map((_,i)=><Cell key={i} fill={COLORS[i%COLORS.length]}/>)}
                </Pie>
                <Tooltip/>
                <Legend/>
              </PieChart>
            </ResponsiveContainer>
          )}
        </section>
      </div>

      {/* Quality metrics */}
      <section className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
        <h2 className="text-lg font-semibold mb-4">🎯 检索质量</h2>
        {quality ? (
          <div className="grid grid-cols-2 gap-6">
            <div className="text-center p-4 bg-green-50 rounded-xl">
              <div className="text-4xl font-bold text-green-600">{(quality.context_recall*100).toFixed(0)}%</div>
              <div className="text-sm text-gray-500 mt-2">Context Recall</div>
              <div className="text-xs text-gray-400">检索到的相关文档比例</div>
            </div>
            <div className="text-center p-4 bg-blue-50 rounded-xl">
              <div className="text-4xl font-bold text-blue-600">{(quality.faithfulness*100).toFixed(0)}%</div>
              <div className="text-sm text-gray-500 mt-2">Faithfulness</div>
              <div className="text-xs text-gray-400">答案忠实于原文的程度</div>
            </div>
          </div>
        ) : (
          <p className="text-gray-400 text-sm">运行 scripts/eval_ragas.py --save-baseline 生成基线</p>
        )}
      </section>

      {/* External tools */}
      <section className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
        <h2 className="text-lg font-semibold mb-4">🔗 外部工具</h2>
        <div className="grid grid-cols-4 gap-4 text-sm">
          <a href={appCfg?.grafana_url||""} target="_blank" rel="noreferrer" className="p-3 border border-gray-200 rounded-lg hover:border-orange-300 hover:bg-orange-50 transition">
            <div className="font-medium text-gray-700">📊 Grafana</div>
            <div className="text-xs text-gray-400 mt-1">Trace / Log / Metric</div>
          </a>
          <a href={appCfg?.langfuse_url||""} target="_blank" rel="noreferrer" className="p-3 border border-gray-200 rounded-lg hover:border-green-300 hover:bg-green-50 transition">
            <div className="font-medium text-gray-700">🔍 Langfuse</div>
            <div className="text-xs text-gray-400 mt-1">LLM 调用链路与成本</div>
          </a>
          <a href={`${appCfg?.cerbos_url||""}`} target="_blank" rel="noreferrer" className="p-3 border border-gray-200 rounded-lg hover:border-blue-300 hover:bg-blue-50 transition">
            <div className="font-medium text-gray-700">🛡️ Cerbos</div>
            <div className="text-xs text-gray-400 mt-1">权限策略引擎</div>
          </a>
          {appCfg?.admin_console_url ? (
            <a href={appCfg.admin_console_url} target="_blank" rel="noreferrer" className="p-3 border border-gray-200 rounded-lg hover:border-purple-300 hover:bg-purple-50 transition">
              <div className="font-medium text-gray-700">🔗 管理台</div>
              <div className="text-xs text-gray-400 mt-1">权限与用户管理</div>
            </a>
          ) : (
            <div className="p-3 border border-gray-100 rounded-lg bg-gray-50 cursor-not-allowed">
              <div className="font-medium text-gray-400">🔗 管理台</div>
              <div className="text-xs text-gray-300 mt-1">未配置</div>
            </div>
          )}
        </div>
      </section>
    </main>
  </div>;
}
