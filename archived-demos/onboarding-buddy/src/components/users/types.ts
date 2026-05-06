import { UserRole } from '@/contexts/AuthContext';

export interface AvatarConfig {
  seed: string;
  customFile: File | null;
  manualMode: boolean;
  skinColor: string;
  hairColor: string;
  topType: string;
  accessoriesType: string;
  facialHairType: string;
  facialHairColor: string;
  eyeType: string;
  eyebrowType: string;
  mouthType: string;
  clotheType: string;
  clotheColor: string;
  graphicType: string;
  noseType: string;
  hatColor: string;
}

// Define the avatar features type with specific string literals
type AvatarFeaturesType = {
  skinColor: string[] | readonly string[];
  hairColor: string[] | readonly string[];
  topType: string[] | readonly string[];
  accessoriesType: string[] | readonly string[];
  facialHairType: string[] | readonly string[];
  facialHairColor: string[] | readonly string[];
  eyeType: string[] | readonly string[];
  eyebrowType: string[] | readonly string[];
  mouthType: string[] | readonly string[];
  clotheType: string[] | readonly string[];
  clotheColor: string[] | readonly string[];
  graphicType: string[] | readonly string[];
  noseType: string[] | readonly string[];
  hatColor: string[] | readonly string[];
};

export type AvatarFeatures = AvatarFeaturesType;

export interface UserFormData {
  name: string;
  email: string;
  role: UserRole;
  managerId: string | undefined;
  avatar: string;
}

export const defaultAvatarFeatures: AvatarFeatures = {
  skinColor: ['#F8D03C', '#EDB98A', '#D08B5B', '#AE5D29', '#614335'],
  hairColor: ['#000000', '#341A12', '#724133', '#A55728', '#B58143', '#C5A48A', '#D6B370', '#F8D03C', '#E6E6E6', '#ECDCBF', '#F5F5F5'],
  topType: [
    'NoHair', 'Eyepatch', 'Hat', 'Hijab', 'Turban', 'WinterHat1', 'WinterHat2',
    'WinterHat3', 'WinterHat4', 'LongHairBigHair', 'LongHairBob', 'LongHairBun',
    'LongHairCurly', 'LongHairCurvy', 'LongHairDreads', 'LongHairFrida',
    'LongHairFro', 'LongHairFroBand', 'LongHairNotTooLong', 'LongHairShavedSides',
    'LongHairMiaWallace', 'LongHairStraight', 'LongHairStraight2', 'LongHairStraightStrand',
    'ShortHairDreads01', 'ShortHairDreads02', 'ShortHairFrizzle', 'ShortHairShaggyMullet',
    'ShortHairShortCurly', 'ShortHairShortFlat', 'ShortHairShortRound', 'ShortHairShortWaved',
    'ShortHairSides', 'ShortHairTheCaesar', 'ShortHairTheCaesarSidePart'
  ],
  accessoriesType: ['Blank', 'Kurt', 'Prescription01', 'Prescription02', 'Round', 'Sunglasses', 'Wayfarers'],
  facialHairType: ['Blank', 'BeardLight', 'BeardMagestic', 'BeardMedium', 'MoustacheFancy', 'MoustacheMagnum'],
  facialHairColor: ['#000000', '#341A12', '#724133', '#A55728', '#B58143', '#C5A48A', '#D6B370'],
  eyeType: ['Close', 'Cry', 'Default', 'Dizzy', 'EyeRoll', 'Happy', 'Hearts', 'Side', 'Squint', 'Surprised', 'Wink', 'WinkWacky', 'Xx'],
  eyebrowType: ['Angry', 'AngryNatural', 'Default', 'DefaultNatural', 'FlatNatural', 'RaisedExcited', 'RaisedExcitedNatural', 'SadConcerned', 'SadConcernedNatural', 'UnibrowNatural', 'UpDown', 'UpDownNatural'],
  mouthType: ['Concerned', 'Default', 'Disbelief', 'Eating', 'Grimace', 'Sad', 'ScreamOpen', 'Serious', 'Smile', 'Tongue', 'Twinkle', 'Vomit'],
  clotheType: ['BlazerShirt', 'BlazerSweater', 'CollarSweater', 'GraphicShirt', 'Hoodie', 'Overall', 'ShirtCrewNeck', 'ShirtScoopNeck', 'ShirtVNeck'],
  clotheColor: ['#000000', '#3D4EFF', '#0033AD', '#65C9FF', '#7A4900', '#E6E6E6', '#D0021B', '#F5F5F5', '#F48120', '#FF5C5C'],
  graphicType: ['Bat', 'Cumbia', 'Deer', 'Diamond', 'Hola', 'Pizza', 'Resist', 'Selena', 'Bear', 'SkullOutline', 'Skull'],
  noseType: ['Default'],
  hatColor: ['#000000', '#3D4EFF', '#0033AD', '#65C9FF', '#7A4900', '#E6E6E6', '#D0021B', '#F5F5F5', '#F48120', '#FF5C5C']
};

export const defaultAvatarConfig: Omit<AvatarConfig, 'seed'> = {
  customFile: null,
  manualMode: false,
  skinColor: defaultAvatarFeatures.skinColor[0],
  hairColor: defaultAvatarFeatures.hairColor[0],
  topType: defaultAvatarFeatures.topType[0],
  accessoriesType: 'Blank',
  facialHairType: 'Blank',
  facialHairColor: defaultAvatarFeatures.hairColor[0],
  eyeType: 'Default',
  eyebrowType: 'Default',
  mouthType: 'Default',
  clotheType: 'ShirtCrewNeck',
  clotheColor: '#65C9FF',
  graphicType: 'Bat',
  noseType: 'Default',
  hatColor: '#000000'
};
