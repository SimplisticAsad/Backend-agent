# ac.project.create  (requirements: requirement.project_management; operations: operation.project.create)
Feature: Create Project succeeds

  Scenario: Create Project succeeds
    Given the user is signed in as Manager
    When Fill in the Project form
    And Create Project
    Then The Project is created and appears in the Project list
