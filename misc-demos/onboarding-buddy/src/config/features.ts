import type { AvatarFeatures } from '@/components/users/types';

// Feature flags for enabling/disabling application features
export const features = {
  // Enable advanced avatar editing features (random generation, customization)
  enableAvatarCustomization: false,
  
  // Enable avatar upload functionality
  enableAvatarUpload: true,

  // Avatar features configuration - explicitly typed to match AvatarFeatures
  avatarFeatures: {
    // Cast to any to avoid readonly array issues
    skinColor: [
      'Tanned', 'Yellow', 'Pale', 'Light', 'Brown', 'DarkBrown', 'Black'
    ] as string[],
    hairColor: [
      'Auburn', 'Black', 'Blonde', 'BlondeGolden', 'Brown', 'BrownDark', 'PastelPink', 'Platinum', 'Red', 'SilverGray'
    ] as string[],
    topType: [
      'NoHair', 'Eyepatch', 'Hat', 'Hijab', 'Turban', 'WinterHat1', 'WinterHat2', 'WinterHat3', 'WinterHat4',
      'LongHairBigHair', 'LongHairBob', 'LongHairBun', 'LongHairCurly', 'LongHairCurvy', 'LongHairDreads',
      'LongHairFrida', 'LongHairFro', 'LongHairFroBand', 'LongHairMiaWallace', 'LongHairNotTooLong',
      'LongHairShavedSides', 'LongHairStraight', 'LongHairStraight2', 'LongHairStraightStrand', 'ShortHairDreads01',
      'ShortHairDreads02', 'ShortHairFrizzle', 'ShortHairShaggyMullet', 'ShortHairShortCurly', 'ShortHairShortFlat',
      'ShortHairShortRound', 'ShortHairShortWaved', 'ShortHairSides', 'ShortHairTheCaesar', 'ShortHairTheCaesarSidePart'
    ] as string[],
    accessoriesType: [
      'Blank', 'Kurt', 'Prescription01', 'Prescription02', 'Round', 'Sunglasses', 'Wayfarers'
    ] as string[],
    facialHairType: [
      'Blank', 'BeardMedium', 'BeardLight', 'BeardMagestic', 'MoustacheFancy', 'MoustacheMagnum'
    ] as string[],
    facialHairColor: [
      'Auburn', 'Black', 'Blonde', 'BlondeGolden', 'Brown', 'BrownDark', 'Platinum', 'Red'
    ] as string[],
    eyeType: [
      'Close', 'Cry', 'Default', 'Dizzy', 'EyeRoll', 'Happy', 'Hearts', 'Side', 'Squint', 'Surprised', 'Wink', 'WinkWacky'
    ] as string[],
    eyebrowType: [
      'Angry', 'AngryNatural', 'Default', 'DefaultNatural', 'FlatNatural', 'RaisedExcited', 'RaisedExcitedNatural',
      'SadConcerned', 'SadConcernedNatural', 'UnibrowNatural', 'UpDown', 'UpDownNatural'
    ] as string[],
    mouthType: [
      'Concerned', 'Default', 'Disbelief', 'Eating', 'Grimace', 'Sad', 'ScreamOpen', 'Serious', 'Smile', 'Tongue', 'Twinkle', 'Vomit'
    ] as string[],
    clotheType: [
      'BlazerShirt', 'BlazerSweater', 'CollarSweater', 'GraphicShirt', 'Hoodie', 'Overall', 'ShirtCrewNeck', 'ShirtScoopNeck', 'ShirtVNeck'
    ] as string[],
    clotheColor: [
      'Black', 'Blue01', 'Blue02', 'Blue03', 'Gray01', 'Gray02', 'Heather', 'PastelBlue', 'PastelGreen', 'PastelOrange', 'PastelRed', 'PastelYellow', 'Pink', 'Red', 'White'
    ] as string[],
    graphicType: [
      'Bat', 'Cumbia', 'Deer', 'Diamond', 'Hola', 'Pizza', 'Resist', 'Selena', 'Bear', 'SkullOutline', 'Skull'
    ] as string[],
    noseType: [
      'Default', 'Small', 'Round', 'Pointy', 'Wide', 'Button', 'Curved', 'Snub', 'Narrow', 'Upturned', 'Freckles'
    ] as string[],
    hatColor: [
      'Black', 'Blue01', 'Blue02', 'Blue03', 'Gray01', 'Gray02', 'Heather', 'PastelBlue', 'PastelGreen', 'PastelOrange', 'PastelRed', 'PastelYellow', 'Pink', 'Red', 'White'
    ] as string[]
  } satisfies AvatarFeatures
} as const;

// AI prompt configuration
export const aiPrompts = {
  personalizedTask: {
    system: `You are an AI assistant specialized in creating detailed and personalized onboarding task descriptions for new hires.
    Your primary function is to take a task template, its assigned difficulty, priority, and estimated effort in days, and generate a comprehensive, actionable task description.
    The description must be tailored to be realistically completable within the given estimated effort, considering the difficulty level.
    Your response MUST consist ONLY of the generated task description, formatted in well-structured markdown. Do not include any preliminary text, conversational filler, or any content outside of the task description itself.
    Avoid making any assumptions about where deliverables should be submitted or stored; focus solely on defining the task and how to approach its completion.`,

    user: (taskTemplate: { name: string; description: string; deliverables: string[] },
           difficulty: number,
           priority: number,
           estimatedEffortDays: number) => { // Added estimatedEffortDays parameter
      const difficultyLevel = difficulty > 3 ? 'challenging' : difficulty > 1 ? 'moderate' : 'straightforward';
      const priorityLevel = priority > 3 ? 'high' : priority > 1 ? 'medium' : 'low'; // Adjusted priority to include 'low' for completeness, assuming priority can be 1.

      return `Please generate a detailed, personalized task description based on the following information.

      Task Template Name: ${taskTemplate.name}
      Task Template Description: ${taskTemplate.description}
      Difficulty: ${difficultyLevel} (${difficulty}/5)
      Priority: ${priorityLevel} (${priority}/5)
      Estimated Effort: ${estimatedEffortDays} day(s)
      Deliverables Expected: ${taskTemplate.deliverables.join(', ')}

      The task description you create must adhere to the following:
      1.  **Scope Definition**: Define a specific, concrete task or mini-project that aligns with the template's purpose, the assigned difficulty, and is reasonably achievable within the ${estimatedEffortDays}-day estimated effort. For example, if the effort is 3 days and difficulty is 'straightforward' for a 'Linux shell script' deliverable, the task should be a simple script, not a complex system administration tool.
      2.  **Clarity and Actionability**: Ensure the description is crystal clear, providing specific, actionable steps or key considerations. The level of detail in steps should reflect the difficulty (more guidance for 'straightforward', more autonomy for 'challenging').
      3.  **Relevance and Importance**: Briefly explain why this task is important, linking its significance to the ${priorityLevel} priority.
      4.  **Deliverables Guidance**: Offer guidance on how to approach creating the specified deliverables (e.g., "Your shell script should focus on X and Y functionality," or "Consider these aspects when preparing your document..."). Do NOT specify where or how the deliverables should be submitted (e.g., avoid "upload to X folder" or "email to Y").
      5.  **Tone**: Maintain an encouraging and supportive tone, suitable for a new hire.
      6.  **Format**: Structure the entire output in clear, well-organized markdown.

      **Critical Output Constraint**: Your response must be *only* the personalized task description itself, in markdown. Do not add any surrounding text, titles like "Extended Task Description:", introductions, or summaries. For instance, if the task name is "Setup Development Environment", the output should start directly with something like "# Task: Setup Your Development Environment" or the first paragraph of the description.`;
    }
  },
  
  assistant: {
    system: `You are an AI onboarding assistant designed to help new hires with their onboarding tasks.
    Your role is to provide helpful, supportive, and encouraging guidance to help them understand and complete their tasks.
    Try to keep your responses short. Be concise, clear, and professional while maintaining a friendly and approachable tone.
    Focus on providing practical advice, clarifying task requirements, and suggesting approaches to complete the tasks.
    If you don't know the answer to something, be honest about it rather than making something up.
    
    IMPORTANT: Your responses should be in plain text only. Do not use any Markdown formatting, code blocks, or special characters.
    - Do not use backticks, asterisks, or any other Markdown syntax
    - Do not create lists with dashes or numbers
    - Write in complete sentences with proper punctuation
    - Keep responses clear and easy to read without formatting`,
    
    context: (task: { name: string; description: string; expandedDescription: string; deliverables: string[]; estimatedEffort: number }) => {
      return `You are helping a new hire with the following task:

Task: ${task.name}
Description: ${task.expandedDescription || task.description}
Estimated Effort: ${task.estimatedEffort} ${task.estimatedEffort === 1 ? 'day' : 'days'}
Deliverables:
${task.deliverables.map(d => `- ${d}`).join('\n')}

Please provide helpful, specific guidance to assist the new hire in understanding and completing this task.`;
    }
  }
} as const;