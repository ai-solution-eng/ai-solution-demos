// src/pages/ItemList.tsx
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import api from "@/lib/api";

interface SupportCase {
  id: number;
  subject: string;
}

export default function CaseList() {
  const [cases, setCases] = useState<SupportCase[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [newSubject, setNewSubject] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    fetchCases();
  }, []);

  const fetchCases = async () => {
    const res = await api.get("/cases");
    console.log("API response data:", res.data);
    console.log("Type of data:", typeof res.data);
    console.log("Is array?", Array.isArray(res.data));
    setCases(res.data);
  };

  const handleSave = async () => {
    await api.post("/cases", { subject: newSubject, msg: newDesc });
    setIsOpen(false);
    setNewSubject("");
    setNewDesc("");
    fetchCases();
  };

  return (
    <div className="p-6">
      <div className="ml-12 max-w-4xl">
        <br/>
        <h1 className="text-2xl mb-4 font-bold text-center">Support Cases</h1>
        <div className="overflow-x-auto w-fit mx-auto">

          <Table className="w-auto border-1 border-[#000000] border-collapse">
            <TableHeader>
              <TableRow className="bg-[#93c5fd]">
                <TableHead className="min-w-[7ch] text-center font-bold border-1 border-[#000000] px-4 py-2">Case ID</TableHead>
                <TableHead className="min-w-[50ch] text-center font-bold border-1 border-[#000000] px-4 py-2">Subject</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {Array.isArray(cases) ? (
                cases.map((item) => (
                  <TableRow
                    key={ item.id}
                    className="cursor-pointer hover:bg-muted border"
                    onClick={() => navigate(`/cases/${item.id}`)}
                  >
                    <TableCell className="min-w-[5ch] custom-text-sm text-center border-1 border-[#000000] px-4 py-2">{item.id}</TableCell>
                    <TableCell className="min-w-[50ch] custom-text-sm border-1 border-[#000000] px-4 py-2">{item.subject}</TableCell>
                  </TableRow>
                ))
              ) : (
                  <TableRow>
                    <TableCell colSpan={2}>No cases available or invalid data</TableCell>
                  </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
        <br/>
        <div className="flex justify-center">
          <Button onClick={() => setIsOpen(true)}>Create New Case</Button>
        </div>
      </div>

      <Dialog open={isOpen} onOpenChange={setIsOpen}>
        <DialogContent className="max-w-[700px] !bg-opacity-100 bg-[#eff6ff] border-2 border-[#000000] rounded-lg shadow-md">
          <DialogHeader>
            <DialogTitle>Create New Case</DialogTitle>
          </DialogHeader>

          <div className="space-y-4 max-w-[600px] w-full mx-auto">
            <div>
              <label htmlFor="name" className="block text-sm font-medium mb-1">
                Subject
              </label>
              <Input className="border-1 border-[#000000] bg-[#ffffff] custom-text-sm"
                id="name"
                value={newSubject}
                onChange={(e) => setNewSubject(e.target.value)}
                placeholder="Enter subject...."
              />
            </div>
            <br/>
            <div>
              <label htmlFor="desc" className="block text-sm font-medium mb-1">
                Description
              </label>
              <Textarea
                id="desc"
                value={newDesc}
                onChange={(e) => setNewDesc(e.target.value)}
                placeholder="Enter description...."
                className="min-h-[180px] border-1 border-[#000000] bg-[#ffffff] custom-text-sm resize-none"
              />
            </div>
            <br/>
            <div className="flex justify-end gap-2 pt-2">
              <Button onClick={handleSave}>Save</Button>&nbsp;&nbsp;
              <Button variant="outline" onClick={() => setIsOpen(false)}>
                Cancel
              </Button>
            </div>
            <br/>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}