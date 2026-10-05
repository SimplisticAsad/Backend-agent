# ac.project_member.create  (requirements: requirement.project_assignment; operations: operation.project_member.create)
Feature: Create Project Member succeeds

  Scenario: Create Project Member succeeds
    Given the user is signed in as Manager
    When Fill in the Project Member form
    And Create Project Member
    Then The Project Member is created and appears in the Project Member list
