// src/components/Header.tsx

import logo from "@/assets/logo.png"; 

export default function Header() {
  return (
      <header className="bg-[#2563eb] p-4 shadow-md">
        <div className="container mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <img src={logo} alt="Logo" className="h-8 w-8 max-w-[100px]" />
            <span className="text-[#ffffff] font-semibold custom-text-xl">&nbsp;&nbsp;&nbsp;Mock Technical Support Application</span>
          </div>
        </div>
      </header>      
  );
}