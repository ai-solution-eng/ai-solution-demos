import { NextRequest, NextResponse } from 'next/server';
import { promises as fs } from 'fs';
import path from 'path';

// The only documents served; static paths let the build trace exactly these two files.
const DOCS: Record<string, string> = {
    'USAGE.md': path.join(process.cwd(), 'USAGE.md'),
    'DIAGRAM.md': path.join(process.cwd(), 'DIAGRAM.md'),
};

export async function GET(
    _request: NextRequest,
    { params }: { params: Promise<{ filename: string }> }
) {
    const { filename } = await params;
    const filePath = Object.hasOwn(DOCS, filename) ? DOCS[filename] : undefined;
    if (!filePath) {
        return NextResponse.json({ status: 'error', message: 'Not found' }, { status: 404 });
    }

    try {
        const content = await fs.readFile(filePath, 'utf8');
        return NextResponse.json({ status: 'success', data: content });
    } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        return NextResponse.json({ status: 'error', message: `Could not read ${filename}: ${detail}` }, { status: 500 });
    }
}
