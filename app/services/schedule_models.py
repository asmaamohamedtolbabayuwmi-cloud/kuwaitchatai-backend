"""Typed output contract for Kuwait University schedule extraction."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StudentData(BaseModel):
    studentId: str | None = None
    name: str | None = None
    college: str | None = None
    major: str | None = None
    minor: str | None = None
    academicLevel: str | None = None
    overallGpa: str | None = None
    majorGpa: str | None = None
    overallGpaWarnings: str | None = None
    majorGpaWarnings: str | None = None
    studentStatus: str | None = None
    honorList: str | None = None


class TermData(BaseModel):
    semester: str | None = None
    academicYear: str | None = None


class CreditsData(BaseModel):
    registered: str | None = None
    completed: str | None = None
    exempted: str | None = None
    transferred: str | None = None
    completedSemesters: str | None = None


class CourseData(BaseModel):
    courseCode: str | None = None
    courseName: str | None = None
    section: str | None = None
    days: str | None = None
    startTime: str | None = None
    endTime: str | None = None
    location: str | None = None
    building: str | None = None
    floor: str | None = None
    area: str | None = None
    room: str | None = None
    instructor: str | None = None
    status: str | None = None
    credits: str | None = None
    finalExamDate: str | None = None
    finalExamStart: str | None = None
    finalExamEnd: str | None = None
    notes: str | None = None


class FingerprintCourseData(BaseModel):
    courseCode: str
    section: str
    status: str


class FingerprintData(BaseModel):
    studentId: str
    academicYear: str
    semester: str
    courses: list[FingerprintCourseData] = Field(default_factory=list)


class ScheduleExtraction(BaseModel):
    documentType: Literal["ku_student_schedule"] = "ku_student_schedule"
    isValid: bool
    invalidReason: str | None = None
    student: StudentData
    term: TermData
    credits: CreditsData
    courses: list[CourseData]
    rawText: str
    fingerprintData: FingerprintData
