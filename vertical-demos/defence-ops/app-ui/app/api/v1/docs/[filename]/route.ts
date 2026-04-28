import { NextRequest, NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';

export async function GET(
    request: NextRequest,
    { params }: { params: Promise<{ filename: string }> }
) {
    const { filename } = await params;
    
    // Security check: only allow USAGE.md and DIAGRAM.md
    if (filename !== 'USAGE.md' && filename !== 'DIAGRAM.md') {
        return NextResponse.json({ status: 'error', message: 'Not found' }, { status: 404 });
    }

    try {
        const filePath = path.join(process.cwd(), filename);
        const content = fs.readFileSync(filePath, 'utf8');
        return NextResponse.json({ status: 'success', data: content });
    } catch (error) {
        return NextResponse.json({ status: 'error', message: `Failed to read file: ${error}` }, { status: 500 });
    }
}
