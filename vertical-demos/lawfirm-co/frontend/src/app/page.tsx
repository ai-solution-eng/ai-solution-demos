'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';

interface Case {
  id: number;
  title: string;
  description: string;
  status: string;
  case_type: string;
  defendant_name: string;
  date_opened: string;
  lead_attorney?: {
    full_name: string;
  };
}

export default function Home() {
  const [cases, setCases] = useState<Case[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [typeFilter, setTypeFilter] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => {
    fetch('/api/cases/')
      .then((res) => res.json())
      .then((data) => {
        setCases(data);
        setLoading(false);
      })
      .catch((err) => {
        console.error('Failed to fetch cases', err);
        setLoading(false);
      });
  }, []);

  // Sort cases by date (newest first) and apply filters
  const filteredAndSortedCases = cases
    .filter(c => statusFilter === 'all' || c.status === statusFilter)
    .filter(c => typeFilter === 'all' || c.case_type === typeFilter)
    .filter(c => {
      if (!searchQuery) return true;
      const query = searchQuery.toLowerCase();
      return (
        c.title.toLowerCase().includes(query) ||
        c.description.toLowerCase().includes(query) ||
        c.defendant_name.toLowerCase().includes(query) ||
        (c.lead_attorney?.full_name || '').toLowerCase().includes(query)
      );
    })
    .sort((a, b) => new Date(b.date_opened).getTime() - new Date(a.date_opened).getTime());

  // Get unique statuses and types for filters
  const uniqueStatuses = Array.from(new Set(cases.map(c => c.status)));
  const uniqueTypes = Array.from(new Set(cases.map(c => c.case_type)));

  return (
    <main className="min-h-screen bg-slate-950 text-slate-100 p-8">
      <header className="mb-12 flex justify-between items-center">
        <div>
          <h1 className="text-4xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-amber-200 to-yellow-500">
            Justitia & Associates
          </h1>
          <p className="text-slate-400 mt-2">Excellence in Crime Prosecution</p>
        </div>
        <div className="flex gap-4">
          <Link href="/admin" className="px-4 py-2 bg-slate-800 rounded-lg hover:bg-slate-700 transition border border-slate-700 text-sm flex items-center">
            Admin
          </Link>
          <Link href="/cases/new" className="px-4 py-2 bg-amber-600 text-slate-900 font-semibold rounded-lg hover:bg-amber-500 transition shadow-lg shadow-amber-900/20">
            New Case
          </Link>
        </div>
      </header>

      <section className="mb-12">
        <div className="flex justify-between items-center mb-6 border-b border-slate-800 pb-4">
          <h2 className="text-2xl font-semibold">Active Cases</h2>
          <div className="flex gap-3">
            <div>
              <label className="text-xs text-slate-500 block mb-1">Search</label>
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search cases..."
                className="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-amber-500"
              />
            </div>

            <div>
              <label className="text-xs text-slate-500 block mb-1">Status</label>
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-amber-500"
              >
                <option value="all">All Statuses</option>
                {uniqueStatuses.map(status => (
                  <option key={status} value={status}>{status}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-xs text-slate-500 block mb-1">Type</label>
              <select
                value={typeFilter}
                onChange={(e) => setTypeFilter(e.target.value)}
                className="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-amber-500"
              >
                <option value="all">All Types</option>
                {uniqueTypes.map(type => (
                  <option key={type} value={type}>{type}</option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {loading ? (
          <div className="text-center py-20 text-slate-500 animate-pulse">Loading secure case files...</div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {filteredAndSortedCases.map((c) => (
              <Link href={`/cases/${c.id}`} key={c.id} className="bg-slate-900/50 border border-slate-800 rounded-xl p-6 hover:border-amber-500/50 transition duration-300 group block">
                <div className="flex justify-between items-start mb-4">
                  <span className={`px-3 py-1 rounded-full text-xs font-medium ${c.status === 'Open' ? 'bg-green-900/30 text-green-400 border border-green-800' :
                    c.status === 'Closed' ? 'bg-slate-800 text-slate-400 border border-slate-700' :
                      'bg-amber-900/30 text-amber-400 border border-amber-800'
                    }`}>
                    {c.status}
                  </span>
                  <span className="text-xs text-slate-500">{new Date(c.date_opened).toLocaleDateString()}</span>
                </div>
                <h3 className="text-xl font-semibold mb-2 group-hover:text-amber-400 transition">{c.title}</h3>
                <p className="text-slate-400 text-sm mb-4 line-clamp-2">{c.description}</p>
                <div className="flex justify-between items-center text-sm text-slate-500 mt-auto pt-4 border-t border-slate-800/50">
                  <span>Type: {c.case_type}</span>
                  <span>Def: {c.defendant_name}</span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </section>

      <section>
        <div className="bg-gradient-to-r from-slate-900 to-slate-800 rounded-2xl p-8 border border-slate-700 relative overflow-hidden">
          <div className="absolute top-0 right-0 w-64 h-64 bg-amber-500/10 rounded-full blur-3xl -mr-32 -mt-32 pointer-events-none"></div>
          <h2 className="text-2xl font-semibold mb-4 relative z-10">Firm Performance</h2>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-8 relative z-10">
            <div>
              <div className="text-4xl font-bold text-white mb-1">94%</div>
              <div className="text-sm text-slate-400">Conviction Rate</div>
            </div>
            <div>
              <div className="text-4xl font-bold text-white mb-1">1,240</div>
              <div className="text-sm text-slate-400">Cases Closed</div>
            </div>
            <div>
              <div className="text-4xl font-bold text-white mb-1">45</div>
              <div className="text-sm text-slate-400">Active Attorneys</div>
            </div>
            <div>
              <div className="text-4xl font-bold text-white mb-1">12</div>
              <div className="text-sm text-slate-400">Pending Trials</div>
            </div>
          </div>
        </div>
      </section>
    </main>
  );
}
