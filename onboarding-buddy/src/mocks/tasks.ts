import { TaskTemplate, PersonalizedTask, TaskReview, TaskDependency } from '../types/tasks';

// Mock task templates
export const mockTaskTemplates: TaskTemplate[] = [
  {
    id: "template-1",
    name: "Introduction to Company Tools",
    description: "Introduction to the tools used within the company",
    deliverables: ["Completed tool access checklist", "Sample work using tools"],
    estimatedEffort: 2,
    createdBy: "1", // Created by admin
    createdByRole: "admin",
    createdAt: new Date("2025-05-01"),
  },
  {
    id: "template-2",
    name: "Code Repository Setup",
    description: "Set up and use the company code repositories",
    deliverables: ["First commit", "Branch creation", "Pull request"],
    estimatedEffort: 1,
    createdBy: "2", // Created by manager
    createdByRole: "manager",
    createdAt: new Date("2025-05-02"),
  },
  {
    id: "template-3",
    name: "Linux Fundamentals",
    description: "Test the new-hire's skills with Linux",
    deliverables: ["Shell script", "Command line exercise results"],
    estimatedEffort: 3,
    createdBy: "2", // Created by manager
    createdByRole: "manager",
    createdAt: new Date("2025-05-03"),
  },
];

// Mock personalized tasks
export const mockPersonalizedTasks: PersonalizedTask[] = [
  // Tasks for new hire (user 4)
  {
    id: "task-4",
    templateId: "template-4",
    name: "API Integration Project",
    description: "Complete the API integration project using our internal framework",
    expandedDescription: `# Task: API Integration Project

This task will help you get hands-on experience with our internal API framework and demonstrate your ability to work with RESTful services.

## Objective
Create a service that integrates with our internal API framework to fetch and process data from a sample endpoint.

## Requirements
- Create a new service using our internal framework
- Implement authentication with the API
- Fetch data from the provided sample endpoint
- Process and transform the data as specified
- Write unit tests for your implementation
- Document your code and create a README

## Deliverables
1. Source code for the service
2. Unit tests with at least 80% coverage
3. API documentation
4. README with setup instructions`,
    deliverables: ["Source code", "Unit tests", "API documentation", "README"],
    assignedTo: "4",
    difficulty: 4,
    priority: 4,
    estimatedEffort: 5,
    status: "completed",
    dependencies: ["task-2"],
    startDate: new Date("2025-05-22"),
    dueDate: new Date("2025-05-29"),
    attachments: [],
    createdBy: "2",
    createdAt: new Date("2025-05-19"),
    completionNotes: "Task completed successfully.",
    completedDate: new Date("2025-05-29"),
    isOnTime: true,
  },
  {
    id: "task-5",
    templateId: "template-5",
    name: "Team Introduction Meetings",
    description: "Schedule and attend introduction meetings with team members",
    expandedDescription: `# Task: Team Introduction Meetings

Building relationships with your team members is crucial for your success. This task will help you get to know your colleagues and understand their roles.

## Objective
Schedule and attend 1:1 introduction meetings with at least 5 team members from different departments.

## Steps
1. Review the team directory to identify key stakeholders
2. Schedule 30-minute meetings with each person
3. Prepare questions in advance
4. Take notes during each meeting
5. Send thank you follow-ups

## Deliverables
1. Meeting schedule with all 5+ meetings completed
2. Brief summary of each meeting (3-5 bullet points per person)
3. Action items from each conversation`,
    deliverables: ["Meeting schedule", "Meeting summaries", "Action items"],
    assignedTo: "4",
    difficulty: 2,
    priority: 3,
    estimatedEffort: 3,
    status: "assigned",
    dependencies: ["task-1"],
    startDate: new Date("2025-05-25"),
    dueDate: new Date("2025-06-01"),
    attachments: [],
    createdBy: "2",
    createdAt: new Date("2025-05-22"),
  },
  // Tasks for another new hire (user 7)
  {
    id: "task-6",
    templateId: "template-6",
    name: "Frontend Component Development",
    description: "Create reusable React components for the design system",
    expandedDescription: `# Task: Frontend Component Development

Contribute to our design system by creating reusable React components that follow our design guidelines.

## Requirements
- Create 3 new reusable components based on the provided designs
- Ensure components are fully responsive
- Write comprehensive unit tests
- Add Storybook documentation
- Follow accessibility best practices

## Deliverables
1. Source code for all components
2. Unit tests with 90%+ coverage
3. Storybook stories
4. Documentation`,
    deliverables: ["Components", "Tests", "Documentation"],
    assignedTo: "7",
    difficulty: 3,
    priority: 4,
    estimatedEffort: 4,
    status: "completed",
    dependencies: [],
    startDate: new Date("2025-05-15"),
    dueDate: new Date("2025-05-25"),
    completedDate: new Date("2025-05-24"),
    isOnTime: true,
    attachments: [],
    createdBy: "5",
    createdAt: new Date("2025-05-10"),
    completionNotes: "Components created and tested successfully.",
    reviewId: "review-2",
  },
  {
    id: "task-7",
    templateId: "template-7",
    name: "Code Review Process",
    description: "Learn and participate in the code review process",
    expandedDescription: `# Task: Code Review Process

Understanding our code review process is essential for maintaining code quality and knowledge sharing.

## Objective
Complete the code review training and participate in at least 3 code reviews.

## Steps
1. Complete the code review training module
2. Review the code review guidelines
3. Shadow 3 code reviews
4. Submit your own code for review
5. Address all feedback received

## Deliverables
1. Completed training certificate
2. Notes from shadowed reviews
3. Link to your first code review submission`,
    deliverables: ["Training certificate", "Review notes", "Code review link"],
    assignedTo: "7",
    difficulty: 2,
    priority: 3,
    estimatedEffort: 2,
    status: "in-progress",
    dependencies: ["task-6"],
    startDate: new Date("2025-05-26"),
    dueDate: new Date("2025-06-02"),
    attachments: [],
    createdBy: "5",
    createdAt: new Date("2025-05-20")
  },
  // Tasks for another new hire (user 6)
  {
    id: "task-8",
    templateId: "template-8",
    name: "Database Schema Design",
    description: "Design a database schema for the new feature",
    expandedDescription: `# Task: Database Schema Design

Design a scalable and efficient database schema for the upcoming feature requirements.

## Requirements
- Review the product requirements document
- Design normalized database tables
- Define relationships and constraints
- Document the schema
- Create migration scripts

## Deliverables
1. ER diagram
2. SQL schema definition
3. Migration scripts
4. Documentation`,
    deliverables: ["ER diagram", "SQL schema", "Migrations", "Documentation"],
    assignedTo: "7",
    difficulty: 4,
    priority: 5,
    estimatedEffort: 3,
    status: "completed",
    dependencies: [],
    startDate: new Date("2025-05-20"),
    dueDate: new Date("2025-05-27"),
    completedDate: new Date("2025-05-27"),
    isOnTime: true,
    attachments: [],
    createdBy: "5",
    createdAt: new Date("2025-05-18"),
    completionNotes: "Schema design completed and reviewed by the team.",
    reviewId: "review-3",
  },
  {
    id: "task-9",
    templateId: "template-9",
    name: "Performance Optimization",
    description: "Identify and fix performance bottlenecks",
    expandedDescription: `# Task: Performance Optimization

Analyze and improve the performance of our main application.

## Objective
Identify and fix at least 3 performance bottlenecks in the application.

## Steps
1. Set up performance monitoring
2. Run benchmarks
3. Identify bottlenecks
4. Implement fixes
5. Measure improvements

## Deliverables
1. Performance analysis report
2. Implemented fixes
3. Benchmark results`,
    deliverables: ["Analysis report", "Code changes", "Benchmark results"],
    assignedTo: "7",
    difficulty: 5,
    priority: 4,
    estimatedEffort: 5,
    status: "assigned",
    dependencies: ["task-8"],
    startDate: new Date("2025-05-28"),
    dueDate: new Date("2025-06-04"),
    attachments: [],
    createdBy: "5",
    createdAt: new Date("2025-05-25"),
  },
  {
    id: "task-1",
    templateId: "template-1",
    name: "Introduction to Company Tools",
    description: "Introduction to the tools used within the company",
    expandedDescription: `
# Task: Getting Started with Company Tools

Welcome to the team! This task is designed to familiarize you with the key tools we use daily. Understanding these tools is crucial for your productivity and collaboration within the company, making this a high-priority task.

**Why is this important?** Efficiently using our company tools will enable you to contribute effectively to your team and projects from day one.

**Task Overview:**

Over the next two days, your goal is to gain a working familiarity with our core toolset. This involves accessing these tools, understanding their basic functionalities, and practicing using them for common tasks.

**Step-by-Step Instructions:**

1.  **Access Request & Setup (Day 1 - Morning):**
    *   Review the "Company Tools Access" document (if provided, otherwise ask your manager for the tools list). This document lists all the tools you'll need access to.
    *   For each tool, follow the instructions to request access. This might involve submitting a request through an internal portal, contacting IT support, or having your manager grant you permissions.
    *   Once access is granted, install any necessary software or configure your accounts according to the provided documentation. Focus on tools such as:
        *   Communication Platforms (e.g., Slack, Microsoft Teams)
        *   Project Management Tools (e.g., Jira, Asana)
        *   Document Collaboration Tools (e.g., Google Workspace, Microsoft Office 365)
    *   Keep track of the tools you've successfully accessed and any issues you encounter.

2.  **Tool Exploration & Practice (Day 1 - Afternoon & Day 2 - Morning):**
    *   For *each* of the core tools, spend approximately 1-2 hours exploring its key features.
    *   **Communication Platform:** Practice sending direct messages, joining channels, and creating a thread. Try setting up your profile with a picture and relevant information.
    *   **Project Management Tool:** Create a sample task, assign it to yourself, and update its status. Explore the different views (e.g., Kanban board, list view).
    *   **Document Collaboration Tool:** Create a new document (e.g., Google Doc, Word document), share it with your manager or mentor, and practice basic formatting and collaboration features (e.g., adding comments, suggesting edits).
    *   Consult internal documentation (e.g., wikis, knowledge bases) or ask colleagues for help if you get stuck. Document any useful tips or shortcuts you discover.

3.  **Troubleshooting & Documentation (Day 2 - Afternoon):**
    *   Review the list of tools you compiled in Step 1 and ensure that you have access to each one. If you're still having trouble with any tool, reach out to IT support or your manager for assistance.
    *   Finalize your "Completed Tool Access Checklist" (see Deliverables section).
    *   Reflect on your experience and identify any tools you feel you need more training on.

**Deliverables:**

1.  **Completed Tool Access Checklist:** This is a simple document (e.g., a spreadsheet or a bulleted list) that confirms you have access to each of the required tools. Include the tool name, the date you gained access, and any notes about the access process (e.g., "Needed to contact IT support," "Access granted by manager").
2.  **Sample Work Using Tools:** This should consist of the artifacts you created while exploring the tools in Step 2. This could be screenshots of your sample task in the project management tool, the document you created in the document collaboration tool, and a brief summary of your activities in the communication platform. The purpose of this deliverable is to demonstrate that you have a basic understanding of how to use the tools.
    `,
    deliverables: ["Completed tool access checklist", "Sample work using tools"],
    assignedTo: "4", // Assigned to new hire
    difficulty: 2,
    priority: 5,
    estimatedEffort: 2,
    status: "completed",
    dependencies: [],
    startDate: new Date("2025-05-15"),
    dueDate: new Date("2025-05-17"),
    attachments: [],
    createdBy: "2", // Created by manager
    createdAt: new Date("2025-05-10"),
    completionNotes: "Task completed successfully.",
    completedDate: new Date("2025-05-17"),
    isOnTime: true,
    reviewId: "review-4",
  },
  {
    id: "task-2",
    templateId: "template-2",
    name: "Code Repository Setup",
    description: "Set up and use the company code repositories",
    expandedDescription: `# Task: Code Repository Setup - Initial Project Setup

This task is designed to get you familiar with our code repository system and workflow. Understanding this process is crucial for collaborating effectively on projects and ensuring code quality, hence the high priority.

**Objective:**

Create a new project within the repository, demonstrating your ability to initialize a project, create a branch, and submit changes for review.

**Steps:**

1.  **Repository Access & Tooling:** Ensure you have access to our primary code repository (e.g., GitHub, GitLab, Bitbucket - instructions for access should be provided separately). Also, confirm that you have Git installed and configured on your local machine. If not, follow the onboarding documentation for installation instructions.
2.  **Project Initialization:** Create a new, *empty* project in the repository. Name it \`onboarding-project-[yourname]\` (e.g., \`onboarding-project-johndoe\`). Include a simple \`README.md\` file in the initial commit with a brief description like "This is my onboarding project."
3.  **Branching:** Create a new branch named \`feature/initial-setup\` from the \`main\` branch. This branch will be used for making changes.
4.  **Code Addition:** Inside your project, create a single file (e.g., \`hello.txt\` or \`hello.py\`). This file should contain a simple line of text or code, such as "Hello, world!" or a basic print statement.
5.  **Commit Changes:** Commit the new file to the \`feature/initial-setup\` branch with a descriptive commit message like "Add initial hello file."
6.  **Push Branch:** Push the \`feature/initial-setup\` branch to the remote repository.
7.  **Create Pull Request:** Create a pull request (PR) from the \`feature/initial-setup\` branch to the \`main\` branch. In the PR description, briefly explain the purpose of the changes.
8.  **Self-Review:** Before requesting a review, take a moment to review your own changes. Ensure the code is clean, the commit message is clear, and the PR description is informative.

**Deliverables:**

*   **First Commit:** The initial commit that created the project with the \`README.md\` file.
*   **Branch Creation:** Successful creation and pushing of the \`feature/initial-setup\` branch.
*   **Pull Request:** A pull request from \`feature/initial-setup\` to \`main\` containing the added file and a clear description.

This task provides a foundational understanding of our code repository workflow. Good luck, and don't hesitate to ask for help if you encounter any issues!`,
    deliverables: ["First commit", "Branch creation", "Pull request"],
    assignedTo: "4", // Assigned to new hire
    difficulty: 3,
    priority: 4,
    estimatedEffort: 1,
    status: "in-progress",
    dependencies: ["task-1"],
    startDate: new Date("2025-05-17"),
    dueDate: new Date("2025-05-18"),
    attachments: [],
    createdBy: "2", // Created by manager
    createdAt: new Date("2025-05-10"),
  },
  {
    id: "task-3",
    templateId: "template-3",
    name: "Linux Fundamentals",
    description: "Test the new-hire's skills with Linux",
    expandedDescription: `# Task: Linux Fundamentals Assessment

This task is designed to assess your foundational Linux skills and introduce you to our team's workflow. Understanding Linux is crucial for your role, as our infrastructure relies heavily on it. This task has a medium priority, ensuring you have a solid base for upcoming projects.

**Objective:**

Demonstrate your proficiency in basic Linux command-line operations and shell scripting by completing the following two exercises:

**Part 1: Command-Line Proficiency**

1.  **Navigation:** Navigate through the file system using commands like \`cd\`, \`pwd\`, and \`ls\`. Practice listing files with different options (e.g., \`-l\`, \`-a\`, \`-t\`). Specifically, create a directory named \`linux_assessment\` in your home directory, and then navigate into it.

2.  **File Manipulation:** Create, copy, move, and delete files and directories using commands like \`touch\`, \`cp\`, \`mv\`, \`rm\`, and \`mkdir\`. Within the \`linux_assessment\` directory, create three empty files named \`file1.txt\`, \`file2.txt\`, and \`file3.txt\`. Then, create a subdirectory named \`backup\`. Copy \`file1.txt\` to the \`backup\` directory. Rename \`file2.txt\` to \`file_renamed.txt\`. Finally, delete \`file3.txt\`.

3.  **File Content Examination:** View and analyze file content using commands like \`cat\`, \`head\`, \`tail\`, \`less\`, and \`grep\`. Use \`echo\` to write the line "This is a test line" to \`file1.txt\`. Then, use \`cat\` to display its contents. Use \`grep\` to search for the word "test" within \`file1.txt\`.

4.  **Permissions:** Understand and modify file permissions using \`chmod\`. Change the permissions of \`file1.txt\` to read-only for the owner.

5. **Redirection:** Redirect standard output and standard error using \`>\`, \`>>\`, \`2>\`, and \`&>\`. Run the command \`ls -l /nonexistent_directory 2> error.log\`. Then view the content of \`error.log\` to understand standard error redirection.

**Deliverable (Part 1):** Create a plain text file named \`command_line_exercise_results.txt\`. In this file, document the commands you used for each step above, along with a brief explanation of what each command does. For example:

\`\`\`
1. Navigation:
   - mkdir linux_assessment: Creates a directory named linux_assessment.
   - cd linux_assessment: Changes the current directory to linux_assessment.
   ...
\`\`\`

**Part 2: Shell Scripting**

1.  **Script Creation:** Create a shell script named \`backup_script.sh\` that automates the process of backing up files.

2.  **Script Functionality:** The script should take a directory as an argument. It should then create a tarball archive of all files in that directory, named \`backup_<directory_name>_<date>.tar.gz\`. The \`<date>\` should be in the format YYYYMMDD.

3.  **Error Handling:** Include basic error handling. For example, check if the provided directory exists. If it doesn't, print an error message and exit.

4.  **Execution:** Make the script executable using \`chmod +x backup_script.sh\`. Test the script by backing up the \`linux_assessment\` directory you created in Part 1.

**Deliverable (Part 2):** Submit the \`backup_script.sh\` file. Ensure the script is well-commented, explaining the purpose of each section of the code.

**Tips for Success:**

*   Break down each task into smaller, manageable steps.
*   Refer to online resources and documentation for assistance.
*   Test your script thoroughly to ensure it functions correctly.
*   Focus on clarity and readability in your deliverables.
*   Don't hesitate to ask questions if you encounter any difficulties.

We look forward to seeing your solutions! This task is designed to be a learning experience, so don't worry about perfection. Focus on demonstrating your understanding of the concepts.
`,
    deliverables: ["Shell script", "Command line exercise results"],
    assignedTo: "4", // Assigned to new hire
    difficulty: 4,
    priority: 3,
    estimatedEffort: 3,
    status: "completed",
    dependencies: ["task-2"],
    startDate: new Date("2025-05-18"),
    dueDate: new Date("2025-05-21"),
    completedDate: new Date("2025-05-20"),
    isOnTime: true,
    attachments: [],
    createdBy: "2", // Created by manager
    createdAt: new Date("2025-05-10"),
    reviewId: "review-1",
    completionNotes: "Task completed successfully.",
  },
];

// Mock task reviews
export const mockTaskReviews: TaskReview[] = [
  {
    id: "review-1",
    taskId: "task-3",
    assignedTo: "3", // Assigned to mentor
    status: "completed",
    score: 10,
    notes: "Excellent work on the Linux fundamentals! Your shell script is well-structured and follows best practices. The command line exercises demonstrate a good understanding of Linux operations.",
    createdBy: "2", // Created by manager
    createdAt: new Date("2025-05-20"),
    completedDate: new Date("2025-05-21"),
  },
  {
    id: "review-2",
    taskId: "task-6",
    assignedTo: "6", // Assigned to another mentor
    status: "completed",
    score: 8,
    notes: "The React components are well-implemented and follow our design system perfectly. Great job on the test coverage and documentation! Just a few minor suggestions in the code review comments.",
    createdBy: "6",
    createdAt: new Date("2025-05-24"),
    completedDate: new Date("2025-05-25"),
  },
  {
    id: "review-3",
    taskId: "task-8",
    assignedTo: "6", // Assigned to another mentor
    status: "in-progress",
    notes: "Initial review: The schema design looks solid overall. I have some questions about the relationship between tables A and B. Also, let's discuss the indexing strategy for the most queried fields.",
    createdBy: "6",
    createdAt: new Date("2025-05-26"),
  },
  {
    id: "review-4",
    taskId: "task-1",
    assignedTo: "3", // Assigned to mentor
    status: "assigned",
    notes: "",
    createdBy: "3",
    createdAt: new Date("2025-05-26"),
  }
];

// Mock task dependencies
export const mockTaskDependencies: TaskDependency[] = [
  {
    taskId: "task-2",
    dependsOnTaskId: "task-1"
  },
  {
    taskId: "task-3",
    dependsOnTaskId: "task-2"
  }
];
