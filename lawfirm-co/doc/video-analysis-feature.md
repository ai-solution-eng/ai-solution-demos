# Video Analysis Feature Implementation

## Overview
Added a new video upload and AI-powered video analysis feature to the lawfirm application. Users can now upload video files (mp4, mkv, mov, webm) to cases and ask questions about the video content using a vision-capable LLM.

## Backend Changes

### 1. Database Models (`backend/app/models.py`)
- Added `CaseVideo` model with fields:
  - `id`: Primary key
  - `filename`: Original filename
  - `file_path`: Server-side storage path
  - `processed`: Processing status flag
  - `summary`: Optional AI-generated summary
  - `created_date`: Upload timestamp
  - `case_id`: Foreign key to Case
- Updated `Case` model to include `videos` relationship

### 2. Schemas (`backend/app/schemas.py`)
- Added `CaseVideoBase`, `CaseVideoCreate`, and `CaseVideo` schemas
- Updated `Case` schema to include `videos: List[CaseVideo]`

### 3. Video Router (`backend/app/routers_video.py`)
New router with the following endpoints:
- `POST /cases/{case_id}/videos` - Upload video file
- `GET /cases/{case_id}/videos` - List all videos for a case
- `DELETE /cases/{case_id}/videos/{video_id}` - Delete a video
- `POST /cases/{case_id}/videos/{video_id}/chat` - Chat with video using vision LLM

Key features:
- Video file validation (mp4, mkv, mov, webm only)
- Frame extraction using OpenCV (extracts 5 key frames)
- Frame compression and base64 encoding for LLM
- Vision model integration for video analysis
- Chat history support

### 4. Dependencies (`backend/requirements.txt`)
Added:
- `opencv-python-headless==4.9.0.80` - For video frame extraction
- `Pillow==10.2.0` - Image processing support

### 5. Main App (`backend/app/main.py`)
- Imported and registered `routers_video` router

## Frontend Changes

### 1. Case Page (`frontend/src/app/cases/[id]/page.tsx`)

#### New Interfaces
- `CaseVideo` interface matching backend schema

#### New State Variables
- `showVideoModal`: Controls video analysis modal visibility
- `videoUploading`: Upload progress indicator
- `selectedVideo`: Currently selected video for analysis
- `videoChatInput`: User input for video questions
- `videoChatHistory`: Chat conversation history
- `videoChatLoading`: Loading state for video chat

#### New Handlers
- `handleUploadVideo`: Handles video file upload
- `handleDeleteVideo`: Deletes a video from the case
- `handleVideoChatSubmit`: Sends questions to the video analysis API

#### UI Components
1. **Video Analysis Modal**:
   - Split-pane interface with video list sidebar and chat area
   - Upload button with file input
   - Video list with selection and delete functionality
   - Chat interface for asking questions about selected video
   - Visual indicators for active video and vision model status

2. **AI Tools Menu**:
   - Added "Video Analysis" button alongside "Dramatis Personae"
   - Blue-themed icon for video analysis

## How It Works

1. **Upload**: User uploads a video file through the modal interface
2. **Storage**: Video is saved to `uploads/videos/` directory with UUID filename
3. **Selection**: User selects a video from the list
4. **Analysis**: When user asks a question:
   - Backend extracts 5 key frames from the video
   - Frames are resized (max 512px) and compressed to JPEG
   - Frames are base64-encoded and sent to vision-capable LLM
   - LLM analyzes the frames and responds to the question
5. **Chat**: Conversation history is maintained for context

## Technical Details

### Frame Extraction
- Uses OpenCV to extract frames at regular intervals
- Maximum 5 frames to balance quality and token usage
- Frames resized to max 512px dimension
- JPEG compression at 70% quality
- Base64 encoding for API transmission

### Vision Model Integration
- Supports OpenAI-compatible vision APIs
- Sends frames as `image_url` content type
- System prompt guides the model for legal video analysis
- Uses the same LLM configuration as other features

### File Management
- Videos stored in `uploads/videos/` directory
- UUID-based filenames prevent conflicts
- File deletion removes both database record and physical file
- Supported formats: mp4, mkv, mov, webm

## UI/UX Features

- **Responsive Design**: Modal adapts to screen size
- **Visual Feedback**: Loading states, hover effects, selection highlights
- **Error Handling**: User-friendly error messages
- **Accessibility**: Proper button labels and ARIA attributes
- **Consistent Styling**: Matches existing dark theme with blue accents

## Future Enhancements

Potential improvements:
- Video thumbnail generation
- Automatic video summarization
- Timeline-based frame selection
- Video playback within the modal
- Batch video upload
- Video transcription integration
- Advanced frame analysis (object detection, face recognition)
