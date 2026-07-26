"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getHealth } from "@/lib/api";

export default function NavBar() {
  const [online, setOnline] = useState<boolean | null>(null);

  useEffect(() => {
    let mounted = true;
    const check = () =>
      getHealth()
        .then(() => mounted && setOnline(true))
        .catch(() => mounted && setOnline(false));

    check();
    const id = setInterval(check, 30_000);
    return () => {
      mounted = false;
      clearInterval(id);
    };
  }, []);

  return (
    <header className="border-b border-gray-800 bg-[#0f1117]/80 backdrop-blur-sm sticky top-0 z-50">
      <div className="max-w-6xl mx-auto px-4 h-14 flex items-center justify-between">
        <Link href="/" className="text-white font-bold text-lg tracking-tight font-mono">
          Momentum Scanner
        </Link>

        <div className="flex items-center gap-6">
          <nav className="flex items-center gap-5 text-sm">
            <Link
              href="/"
              className="text-gray-300 hover:text-white transition-colors"
            >
              Today
            </Link>
            <Link
              href="/history"
              className="text-gray-300 hover:text-white transition-colors"
            >
              History
            </Link>
          </nav>

          <div className="flex items-center gap-2 text-xs font-mono">
            <span
              className={`w-2 h-2 rounded-full ${
                online === null
                  ? "bg-gray-500"
                  : online
                  ? "bg-green-500"
                  : "bg-red-500"
              }`}
            />
            <span className={online === null ? "text-gray-600" : online ? "text-green-500" : "text-red-400"}>
              {online === null ? "checking" : online ? "API connected" : "API offline"}
            </span>
          </div>
        </div>
      </div>
    </header>
  );
}
