import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import api from "@/lib/api";

import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";

import { Button } from "@/components/ui/button";

type CaseDetail = {
  id: number | string;
  msg_type: string;
  msg: string;
};

export default function ItemDetails() {
  const { id } = useParams();
  const [details, setDetails] = useState<CaseDetail[]>([]);

  useEffect(() => {
    api.get(`/cases/${id}/details`).then(res => setDetails(res.data));
  }, [id]);

  return (
    <div>
      <br/>
      <h1 className="text-2xl mb-4 font-bold text-center">Support Case #{id}</h1>

      <div className="overflow-x-auto w-fit mx-auto">
        <Table className="w-full table-auto border-collapse border-1 border-[#000000]">
          <TableHeader className="bg-gray-100">
            <TableRow className="bg-[#dbeafe]">
              <TableHead className="px-4 py-2 text-center border-1 border-[#000000]">Message Type</TableHead>
              <TableHead className="px-4 py-2 text-center border-1 border-[#000000]">Message</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {details.map((detail) => (
              <TableRow key={detail.id}>
                <TableCell className="px-4 py-2 border-1 border-[#000000] custom-text-sm">
                  {detail.msg_type}
                </TableCell>
                <TableCell className="px-4 py-2 border-1 border-[#000000] max-w-[100ch] custom-text-sm whitespace-normal break-words">
                  {detail.msg}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <br/>
      <div className="mt-6 flex justify-center">
        <Link to="/">
          <Button>&larr; Back</Button>
        </Link>
      </div>

    </div>
  );
}

