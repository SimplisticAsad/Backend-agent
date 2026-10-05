# ac.project.delete  (requirements: requirement.project_management; operations: operation.project.delete)
Feature: Delete Project succeeds

  Scenario: Delete Project succeeds
    Given the user is signed in as Manager
    And the record being worked on exists
    When Confirm deleting the Project
    And Delete Project
    Then The Project is removed from the Project list
